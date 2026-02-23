import os
import time
import threading
from urllib.parse import unquote
import requests
from io import BytesIO
from PIL import Image
import psycopg2
from psycopg2.extras import RealDictCursor
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
# --- Configurations ---
DB_URI = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_API_URL = "https://public-api.wordpress.com/rest/v1.1/sites/pranavcea.wordpress.com/posts/?number=50"
ALLPOST_API_URL = "https://blog.pranavblog.online/allpost"
FRONTEND_URL = "https://blog.pranavblog.online"

CACHE_DIR = "cache"
IMAGE_DIR = os.path.join(CACHE_DIR, "images")
CACHED_HTML_PATH = os.path.join(CACHE_DIR, "cached_index.html")
LOCAL_TAILWIND_JS = os.path.join(CACHE_DIR, "tailwind-local.js")
MAX_CACHE_SIZE_MB = 300

# Ensure directories exist
os.makedirs(IMAGE_DIR, exist_ok=True)

app = FastAPI(title="To The Point - SSR Engine")
app.mount("/cache", StaticFiles(directory="cache"), name="cache")

# --- Helper Functions ---
def get_alt_text(title):
    alt = f"Illustration of {title}"
    if len(alt) > 50:
        alt = alt[:47] + "..."
    return alt.replace('"', '&quot;') # Safe for HTML attribute

def generate_default_images():
    feat_path = os.path.join(IMAGE_DIR, "default_feat.webp")
    grid_path = os.path.join(IMAGE_DIR, "default_grid.webp")
    # Creates a light green placeholder if an image fails to load
    if not os.path.exists(feat_path):
        img = Image.new('RGB', (800, 450), color=(220, 252, 231)) # Tailwind green-100
        img.save(feat_path, "WEBP", quality=80)
    if not os.path.exists(grid_path):
        img = Image.new('RGB', (300, 300), color=(220, 252, 231))
        img.save(grid_path, "WEBP", quality=80)

# --- Database Setup ---
def get_db_connection():
    return psycopg2.connect(DB_URI, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS homedata (
            id SERIAL PRIMARY KEY,
            wp_id BIGINT UNIQUE,
            title TEXT,
            slug TEXT,
            original_url TEXT,
            date TIMESTAMP,
            excerpt TEXT,
            original_image_url TEXT,
            featured_image_path TEXT,
            grid_image_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Safely adding BYTEA columns to store compressed images in DB
    try:
        cursor.execute("ALTER TABLE homedata ADD COLUMN featured_image_data BYTEA")
        conn.commit()
    except Exception:
        conn.rollback()

    try:
        cursor.execute("ALTER TABLE homedata ADD COLUMN grid_image_data BYTEA")
        conn.commit()
    except Exception:
        conn.rollback()

    cursor.close()
    conn.close()
    print("Database initialized successfully with image byte support.")
# --- Image Compression Engine ---
def compress_image(image_url, wp_id):
    if not image_url: return None, None, None, None
    try:
        response = requests.get(image_url, timeout=15)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content)).convert("RGB")
        
        feat_filename = f"feat_{wp_id}.webp"
        feat_path = os.path.join(IMAGE_DIR, feat_filename)
        feat_img = img.copy()
        feat_img.thumbnail((800, 450), Image.Resampling.LANCZOS)
        save_with_target_size(feat_img, feat_path, target_kb=30)
        with open(feat_path, "rb") as f: feat_bytes = f.read()

        grid_filename = f"grid_{wp_id}.webp"
        grid_path = os.path.join(IMAGE_DIR, grid_filename)
        grid_img = img.copy()
        grid_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        save_with_target_size(grid_img, grid_path, target_kb=10)
        with open(grid_path, "rb") as f: grid_bytes = f.read()

        return feat_filename, grid_filename, feat_bytes, grid_bytes
    except Exception as e:
        print(f"Error compressing image {image_url}: {e}")
        return None, None, None, None
def save_with_target_size(img, path, target_kb):
    quality = 85
    while quality > 10:
        img.save(path, "WEBP", quality=quality)
        size_kb = os.path.getsize(path) / 1024
        if size_kb <= target_kb:
            break
        quality -= 5

# --- Cache Management (300MB Limit) ---
def enforce_cache_limit():
    total_size = sum(os.path.getsize(os.path.join(IMAGE_DIR, f)) for f in os.listdir(IMAGE_DIR) if os.path.isfile(os.path.join(IMAGE_DIR, f)))
    total_size_mb = total_size / (1024 * 1024)
    
    if total_size_mb > MAX_CACHE_SIZE_MB:
        print(f"Cache size ({total_size_mb:.2f}MB) exceeded limit. Cleaning up old images...")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT featured_image_path, grid_image_path FROM homedata ORDER BY date DESC LIMIT 50")
        recent_images = set()
        for row in cursor.fetchall():
            if row['featured_image_path']: recent_images.add(os.path.basename(row['featured_image_path']))
            if row['grid_image_path']: recent_images.add(os.path.basename(row['grid_image_path']))
        
        # Keep defaults safe
        recent_images.add("default_feat.webp")
        recent_images.add("default_grid.webp")

        for f in os.listdir(IMAGE_DIR):
            if f not in recent_images:
                try: os.remove(os.path.join(IMAGE_DIR, f))
                except: pass
        conn.close()

# --- Data Sync Logic ---
def sync_data():
    print("Starting data sync and HTML generation...")
    try:
        mapping_res = requests.get(ALLPOST_API_URL, timeout=10)
        mappings = {unquote(item['original_url']): item['slug'] for item in mapping_res.json()}
    except Exception as e:
        print(f"Failed to fetch mappings: {e}")
        mappings = {}

    try:
        wp_res = requests.get(WP_API_URL, timeout=10)
        posts = wp_res.json().get('posts', [])
    except Exception as e:
        print(f"Failed to fetch WP posts: {e}")
        posts = []

    conn = get_db_connection()
    cursor = conn.cursor()

    for post in posts:
        wp_id = post['ID']
        title = post['title']
        original_url = unquote(post['URL'])
        slug = mappings.get(original_url, f"post-{wp_id}")
        date = post['date']
        excerpt = post.get('excerpt', '')
        
        image_url = post.get('featured_image')
        if not image_url and post.get('content'):
            img_soup = BeautifulSoup(post['content'], 'html.parser')
            img_tag = img_soup.find('img')
            if img_tag: image_url = img_tag.get('src')
        
        cursor.execute("SELECT id, featured_image_data FROM homedata WHERE wp_id = %s", (wp_id,))
        existing_post = cursor.fetchone()

        if existing_post:
            # Puraane broken posts ko recover karna jinke DB me image bytes nahi hai
            if existing_post['featured_image_data'] is None and image_url:
                f_name, g_name, f_bytes, g_bytes = compress_image(image_url, wp_id)
                if f_bytes:
                    cursor.execute("""
                        UPDATE homedata SET featured_image_data = %s, grid_image_data = %s, featured_image_path = %s, grid_image_path = %s WHERE wp_id = %s
                    """, (f_bytes, g_bytes, f_name, g_name, wp_id))
                    conn.commit()
            continue

        f_name, g_name, f_bytes, g_bytes = compress_image(image_url, wp_id)

        cursor.execute("""
            INSERT INTO homedata (wp_id, title, slug, original_url, date, excerpt, original_image_url, featured_image_path, grid_image_path, featured_image_data, grid_image_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wp_id) DO NOTHING
        """, (wp_id, title, slug, original_url, date, excerpt, image_url, f_name, g_name, f_bytes, g_bytes))    
    conn.commit()
    cursor.close()
    conn.close()
    
    enforce_cache_limit()
    generate_static_html()
    print("Data sync complete!")

# --- Server Side Template Generation ---
def generate_static_html():
    if not os.path.exists("index.html"):
        print("index.html not found in root directory!")
        return

    # Download Tailwind JIT locally
    if not os.path.exists(LOCAL_TAILWIND_JS):
        try:
            tailwind_script = requests.get("https://cdn.tailwindcss.com").text
            with open(LOCAL_TAILWIND_JS, "w", encoding="utf-8") as f:
                f.write(tailwind_script)
        except Exception as e:
            print("Could not download Tailwind JS", e)

    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # Add standard meta description if missing for SEO
    if not soup.find('meta', attrs={'name': 'description'}):
        meta_desc = soup.new_tag('meta', attrs={'name': 'description', 'content': 'Latest insights and articles on Environment, Energy, and Agriculture. Click and read more.'})
        soup.head.append(meta_desc)

    # Fix accessibility for Next/Prev buttons
    prev_btn = soup.find('button', attrs={'onclick': 'prevSlide()'})
    if prev_btn and not prev_btn.has_attr('aria-label'):
        prev_btn['aria-label'] = "Previous Slide"
        
    next_btn = soup.find('button', attrs={'onclick': 'nextSlide()'})
    if next_btn and not next_btn.has_attr('aria-label'):
        next_btn['aria-label'] = "Next Slide"

    # Replace external Tailwind CDN with Local Cached Script
    # Replace external Tailwind CDN with Local Cached Script AND add 'defer' to fix Render-Blocking error
    for script in soup.find_all('script'):
        if script.get('src') and 'tailwindcss.com' in script.get('src'):
            script['src'] = "/cache/tailwind-local.js"
            script['defer'] = "defer" # HTML loading block nahi karega
            break            
    # Fetch data from DB
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM homedata ORDER BY date DESC LIMIT 20")
    posts = cursor.fetchall()
    conn.close()

    slider_posts = posts[:8]
    grid_posts = posts[8:20]

    # Populate Slider
    slider_track = soup.find(id="sliderTrack")
    indicators_container = soup.find(id="slideIndicators")
    
    if slider_track:
        slider_track.clear()
        if indicators_container: indicators_container.clear()
        
        for i, post in enumerate(slider_posts):
            # STRICTLY LOCAL IMAGE HANDLING & PATH FIX
            db_path = post['featured_image_path']
            main_img = f"/img/{os.path.basename(db_path)}" if db_path else '/img/default_feat.webp'
            alt_text = get_alt_text(post['title'])

            slide_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="w-full flex-shrink-0 relative h-full block animate-fade-in">
                <img src="{main_img}" class="w-full h-full object-cover" alt="{alt_text}">
                <div class="slide-overlay absolute inset-0 flex flex-col justify-end p-6 md:p-10">
                    <span class="bg-green-600/90 backdrop-blur text-white text-[0.65rem] uppercase tracking-widest font-bold px-2 py-1 rounded w-fit mb-3 border border-white/20">Featured</span>
                    <h2 class="text-white text-xl md:text-3xl font-bold leading-tight drop-shadow-lg line-clamp-2 mb-1">{post['title']}</h2>
                    <div class="h-1 w-12 bg-green-400 rounded-full mt-2 mb-1"></div>
                </div>
            </a>
            """
            slider_track.append(BeautifulSoup(slide_html, 'html.parser'))

            # Indicator dots - added aria-label for accessibility
            # Indicator dots - Touch removed, purely visual to fix PageSpeed Touch Target error
            active_class = "bg-white w-6" if i == 0 else "bg-white/40 w-1.5"
            dot_html = f'<div class="h-1.5 rounded-full transition-all duration-300 {active_class}"></div>'
            if indicators_container:
                indicators_container.append(BeautifulSoup(dot_html, 'html.parser'))
    # Remove skeleton loaders
    skeleton = soup.find(id="sliderSkeleton")
    if skeleton: skeleton.decompose()
    for skel in soup.find_all(class_="skeleton-card"):
        skel.decompose()

    # Populate Grid
    posts_grid = soup.find(id="postsGrid")
    for post in grid_posts:
            formatted_date = post['date'].strftime('%m/%d/%Y') if post['date'] else ''
            
            # STRICTLY LOCAL IMAGE HANDLING & PATH FIX
            db_path = post['grid_image_path']
            main_img = f"/img/{os.path.basename(db_path)}" if db_path else '/img/default_grid.webp'
            alt_text = get_alt_text(post['title'])

            card_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline">
                <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                    <img src="{main_img}" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="{alt_text}">
                </div>
                <div class="flex flex-col justify-center flex-1 min-w-0">
                    <h3 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2 group-hover:text-green-700 transition-colors">{post['title']}</h3>
                    <div class="flex items-center gap-2 text-xs text-green-700/80 mb-2">
                        <i class="far fa-calendar"></i>
                        <span>{formatted_date}</span>
                    </div>
                    <span class="text-xs text-green-600 font-medium group-hover:underline flex items-center gap-1">
                        Read Article <i class="fas fa-arrow-right text-[10px]"></i>
                    </span>
                </div>
            </a>
            """
            posts_grid.append(BeautifulSoup(card_html, 'html.parser'))

    # Setup Load More Spinner UI for Infinite Scroll
    load_more_div = soup.find(id="loadMoreContainer")
    if load_more_div:
        load_more_div.clear()
        # Clean spinner UI without button
        spinner_html = '<div class="spinner mx-auto mb-2 border-green-800"></div><p class="text-sm text-green-700 font-bold">Loading more articles...</p>'
        load_more_div.append(BeautifulSoup(spinner_html, 'html.parser'))
        load_more_div['class'] = load_more_div.get('class', []) + ['hidden']

    # Inject Clean SSR specific JS Logic for Infinite Scroll & Slider
    scripts = soup.find_all('script')
    for script in scripts:
        if script.string and ('fetchAndAppend' in script.string or 'BACKEND_API' in script.string):
            script.decompose()

    clean_js = f"""
    <script>
        let currentSlideIndex = 0;
        const totalSlides = {len(slider_posts)};
        const sliderTrack = document.getElementById('sliderTrack');
        const indicatorsContainer = document.getElementById('slideIndicators');
        let autoSlideInterval;

        window.goToSlide = (index) => {{
            currentSlideIndex = index;
            updateSlider();
            resetTimer();
        }};

        window.nextSlide = () => {{
            if (totalSlides === 0) return;
            currentSlideIndex = (currentSlideIndex + 1) % totalSlides;
            updateSlider();
            resetTimer();
        }};

        window.prevSlide = () => {{
            if (totalSlides === 0) return;
            currentSlideIndex = (currentSlideIndex - 1 + totalSlides) % totalSlides;
            updateSlider();
            resetTimer();
        }};

        function updateSlider() {{
            const percentage = currentSlideIndex * -100;
            if(sliderTrack) sliderTrack.style.transform = `translateX(${{percentage}}%)`;
            if (indicatorsContainer) {{
                const dots = indicatorsContainer.children;
                for (let i = 0; i < dots.length; i++) {{
                    dots[i].className = (i === currentSlideIndex) 
                        ? 'h-1.5 rounded-full bg-white w-6 transition-all duration-300' 
                        : 'h-1.5 rounded-full bg-white/40 w-1.5 hover:bg-white transition-all duration-300';
                }}
            }}
        }}

        function startAutoSlide() {{
            if(autoSlideInterval) clearInterval(autoSlideInterval);
            autoSlideInterval = setInterval(() => {{
                if (totalSlides > 1) {{
                    currentSlideIndex = (currentSlideIndex + 1) % totalSlides;
                    updateSlider();
                }}
            }}, 3000); 
        }}

        function resetTimer() {{
            clearInterval(autoSlideInterval);
            startAutoSlide();
        }}
        
        startAutoSlide(); // Initialize

        // INFINITE SCROLL LOGIC
        let isLoading = false;
        let noMorePosts = false;
        const loadMoreContainer = document.getElementById('loadMoreContainer');

        window.addEventListener('scroll', async () => {{
            if (isLoading || noMorePosts) return;
            
            // Check if user has scrolled near bottom (buffer of 500px)
            const {{ scrollTop, scrollHeight, clientHeight }} = document.documentElement;
            if (scrollTop + clientHeight >= scrollHeight - 500) {{
                await fetchMorePosts();
            }}
        }});

        async function fetchMorePosts() {{
            isLoading = true;
            if (loadMoreContainer) loadMoreContainer.classList.remove('hidden');
            
            const currentCount = document.querySelectorAll('#postsGrid > a').length;
            
            try {{
                const res = await fetch(`/more?offset=${{currentCount + 8}}`);
                const html = await res.text();
                
                if (html.trim() === "") {{
                    noMorePosts = true;
                    if (loadMoreContainer) {{
                        loadMoreContainer.innerHTML = '<p class="text-sm text-green-700 font-bold opacity-70">No more articles to load.</p>';
                    }}
                }} else {{
                    document.getElementById('postsGrid').insertAdjacentHTML('beforeend', html);
                }}
            }} catch(e) {{
                console.error(e);
            }} finally {{
                isLoading = false;
                if (!noMorePosts && loadMoreContainer) loadMoreContainer.classList.add('hidden');
            }}
        }}
    </script>
    """
    soup.body.append(BeautifulSoup(clean_js, 'html.parser'))

    # Save to Cache File
    with open(CACHED_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(str(soup))


# --- Background Thread ---
def background_task_runner():
    while True:
        try:
            sync_data()
        except Exception as e:
            print(f"Background Task Error: {e}")
        time.sleep(3600)

# --- Endpoints ---
@app.on_event("startup")
def startup_event():
    init_db()
    generate_default_images() # Generates local fallbacks instantly
    
    # FORCE HTML generation so old /cache/images paths are replaced with new /img/ paths!
    generate_static_html()
    
    thread = threading.Thread(target=background_task_runner, daemon=True)
    thread.start()
@app.get("/", response_class=HTMLResponse)
async def serve_homepage():
    if os.path.exists(CACHED_HTML_PATH):
        with open(CACHED_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1 style='text-align:center; padding-top: 50px; font-family: sans-serif; color: #166534;'>Optimizing Site Engine... Please refresh in 10 seconds.</h1>")

@app.get("/more", response_class=HTMLResponse)
async def load_more(offset: int = 20):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM homedata ORDER BY date DESC LIMIT 12 OFFSET %s", (offset,))
    posts = cursor.fetchall()
    conn.close()

    html_snippets = ""
    for post in posts:
        formatted_date = post['date'].strftime('%m/%d/%Y') if post['date'] else ''
        
        # STRICTLY LOCAL IMAGE HANDLING & PATH FIX
        db_path = post['grid_image_path']
        main_img = f"/img/{os.path.basename(db_path)}" if db_path else '/img/default_grid.webp'
        alt_text = get_alt_text(post['title'])

        html_snippets += f"""
        <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline">
            <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                <img src="{main_img}" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="{alt_text}">
            </div>
            <div class="flex flex-col justify-center flex-1 min-w-0">
                <h3 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2 group-hover:text-green-700 transition-colors">{post['title']}</h3>
                <div class="flex items-center gap-2 text-xs text-green-700/80 mb-2">
                    <i class="far fa-calendar"></i>
                    <span>{formatted_date}</span>
                </div>
                <span class="text-xs text-green-600 font-medium group-hover:underline flex items-center gap-1">
                    Read Article <i class="fas fa-arrow-right text-[10px]"></i>
                </span>
            </div>
        </a>
        """
    return html_snippets
@app.get("/img/{filename}")
async def get_image(filename: str):
    filepath = os.path.join(IMAGE_DIR, filename)
    
    # Cache-Control header set to 1 year (31536000 sec) to fix PageSpeed "Efficient Cache Lifetimes"
    cache_headers = {"Cache-Control": "public, max-age=31536000"}

    # Check if cache exists locally (Server has not restarted)
    if os.path.exists(filepath):
        return FileResponse(filepath, headers=cache_headers)

    # Server restarted! Cache lost. Recovering from Database...
    conn = get_db_connection()
    cursor = conn.cursor()

    if filename == "default_feat.webp" or filename == "default_grid.webp":
        generate_default_images()
        return FileResponse(filepath, headers=cache_headers)

    is_feat = filename.startswith("feat_")
    try:
        wp_id = int(filename.split("_")[1].split(".")[0])
    except:
        raise HTTPException(status_code=404, detail="Image not found")

    col = "featured_image_data" if is_feat else "grid_image_data"
    cursor.execute(f"SELECT {col} FROM homedata WHERE wp_id = %s", (wp_id,))
    row = cursor.fetchone()
    conn.close()

    if row and row[col]:
        # Save bytes back to cache folder so next request is fast!
        with open(filepath, "wb") as f:
            f.write(row[col])
        return FileResponse(filepath, headers=cache_headers)

    raise HTTPException(status_code=404, detail="Image not found")
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
