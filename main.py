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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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
    conn.commit()
    cursor.close()
    conn.close()
    print("Database initialized successfully.")

# --- Image Compression Engine ---
def compress_image(image_url, wp_id):
    if not image_url: return None, None
    try:
        response = requests.get(image_url, timeout=15)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content)).convert("RGB")
        
        # 1. Compress for Slider (~30KB) - Resolution 800x450
        feat_filename = f"feat_{wp_id}.webp"
        feat_path = os.path.join(IMAGE_DIR, feat_filename)
        feat_img = img.copy()
        feat_img.thumbnail((800, 450), Image.Resampling.LANCZOS)
        save_with_target_size(feat_img, feat_path, target_kb=30)

        # 2. Compress for Grid (~10KB) - Resolution 300x300
        grid_filename = f"grid_{wp_id}.webp"
        grid_path = os.path.join(IMAGE_DIR, grid_filename)
        grid_img = img.copy()
        grid_img.thumbnail((300, 300), Image.Resampling.LANCZOS)
        save_with_target_size(grid_img, grid_path, target_kb=10)

        return f"/cache/images/{feat_filename}", f"/cache/images/{grid_filename}"
    except Exception as e:
        print(f"Error compressing image {image_url}: {e}")
        return None, None

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
        cursor.execute("SELECT id FROM homedata WHERE wp_id = %s", (wp_id,))
        if cursor.fetchone():
            continue 

        title = post['title']
        original_url = unquote(post['URL'])
        slug = mappings.get(original_url, f"post-{wp_id}")
        date = post['date']
        excerpt = post.get('excerpt', '')
        
        # Get Image properly
        image_url = post.get('featured_image')
        if not image_url and post.get('content'):
            img_soup = BeautifulSoup(post['content'], 'html.parser')
            img_tag = img_soup.find('img')
            if img_tag: image_url = img_tag.get('src')
        
        feat_path, grid_path = compress_image(image_url, wp_id)

        cursor.execute("""
            INSERT INTO homedata (wp_id, title, slug, original_url, date, excerpt, original_image_url, featured_image_path, grid_image_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (wp_id) DO NOTHING
        """, (wp_id, title, slug, original_url, date, excerpt, image_url, feat_path, grid_path))
    
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

    # Download Tailwind JIT locally to avoid external CDN requests by the user
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

    # 1. Replace external Tailwind CDN with Local Cached Script
    for script in soup.find_all('script'):
        if script.get('src') and 'tailwindcss.com' in script.get('src'):
            script['src'] = "/cache/tailwind-local.js"
            break
            
    # Add Noscript Fallback for disabled JS
    noscript = soup.new_tag('noscript')
    noscript_link = soup.new_tag('link', rel="stylesheet", href="https://cdnjs.cloudflare.com/ajax/libs/tailwindcss/2.2.19/tailwind.min.css")
    noscript.append(noscript_link)
    soup.head.append(noscript)

    # 2. Fetch data from DB
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM homedata ORDER BY date DESC LIMIT 20")
    posts = cursor.fetchall()
    conn.close()

    slider_posts = posts[:8]
    grid_posts = posts[8:20]

    # 3. Populate Slider
    slider_track = soup.find(id="sliderTrack")
    indicators_container = soup.find(id="slideIndicators")
    
    if slider_track:
        slider_track.clear()
        if indicators_container: indicators_container.clear()
        
        for i, post in enumerate(slider_posts):
            # Fallbacks images setup
            main_img = post['featured_image_path'] or post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'
            fallback_img = post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'

            slide_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="w-full flex-shrink-0 relative h-full block animate-fade-in">
                <img src="{main_img}" onerror="this.src='{fallback_img}'" class="w-full h-full object-cover" alt="Featured">
                <div class="slide-overlay absolute inset-0 flex flex-col justify-end p-6 md:p-10">
                    <span class="bg-green-600/90 backdrop-blur text-white text-[0.65rem] uppercase tracking-widest font-bold px-2 py-1 rounded w-fit mb-3 border border-white/20">Featured</span>
                    <h2 class="text-white text-xl md:text-3xl font-bold leading-tight drop-shadow-lg line-clamp-2 mb-1">{post['title']}</h2>
                    <div class="h-1 w-12 bg-green-400 rounded-full mt-2 mb-1"></div>
                </div>
            </a>
            """
            slider_track.append(BeautifulSoup(slide_html, 'html.parser'))

            # Inject Indicator dots directly
            active_class = "bg-white w-6" if i == 0 else "bg-white/40 w-1.5 hover:bg-white"
            dot_html = f'<button class="h-1.5 rounded-full transition-all duration-300 {active_class}" onclick="goToSlide({i})"></button>'
            if indicators_container:
                indicators_container.append(BeautifulSoup(dot_html, 'html.parser'))

    # Remove skeleton loaders
    skeleton = soup.find(id="sliderSkeleton")
    if skeleton: skeleton.decompose()
    for skel in soup.find_all(class_="skeleton-card"):
        skel.decompose()

    # 4. Populate Grid
    posts_grid = soup.find(id="postsGrid")
    if posts_grid:
        posts_grid.clear()
        for post in grid_posts:
            formatted_date = post['date'].strftime('%m/%d/%Y') if post['date'] else ''
            main_img = post['grid_image_path'] or post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'
            fallback_img = post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'

            card_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline">
                <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                    <img src="{main_img}" onerror="this.src='{fallback_img}'" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="Thumbnail">
                </div>
                <div class="flex flex-col justify-center flex-1 min-w-0">
                    <h4 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2 group-hover:text-green-700 transition-colors">{post['title']}</h4>
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

    # 5. Inject Clean SSR specific JS Logic
    # Remove original large logic script block
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
            sliderTrack.style.transform = `translateX(${{percentage}}%)`;
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

        // SSR Load More
        window.loadMorePosts = async function() {{
            const btn = document.getElementById('loadMoreBtn');
            const spinner = document.getElementById('btnSpinner');
            btn.disabled = true;
            spinner.classList.remove('hidden');
            btn.querySelector('span').innerText = "Loading...";
            
            const currentCount = document.querySelectorAll('#postsGrid > a').length;
            
            try {{
                const res = await fetch(`/more?offset=${{currentCount + 8}}`);
                const html = await res.text();
                if (html.trim() === "") {{
                    document.getElementById('loadMoreContainer').classList.add('hidden');
                }} else {{
                    document.getElementById('postsGrid').insertAdjacentHTML('beforeend', html);
                }}
            }} catch(e) {{
                console.error(e);
            }} finally {{
                btn.disabled = false;
                spinner.classList.add('hidden');
                btn.querySelector('span').innerText = "View More Articles";
            }}
        }};
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
    thread = threading.Thread(target=background_task_runner, daemon=True)
    thread.start()
    
    # Guarantee first HTML build if it doesn't exist
    if not os.path.exists(CACHED_HTML_PATH):
        sync_data()

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
        main_img = post['grid_image_path'] or post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'
        fallback_img = post['original_image_url'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'

        html_snippets += f"""
        <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline">
            <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                <img src="{main_img}" onerror="this.src='{fallback_img}'" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="Thumbnail">
            </div>
            <div class="flex flex-col justify-center flex-1 min-w-0">
                <h4 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2 group-hover:text-green-700 transition-colors">{post['title']}</h4>
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
