import os
import time
import json
import shutil
import threading
import requests
from io import BytesIO
from datetime import datetime
from PIL import Image
import psycopg2
from psycopg2.extras import RealDictCursor
from bs4 import BeautifulSoup
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# --- Configurations ---
DB_URI = "postgres://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"
WP_API_URL = "https://public-api.wordpress.com/rest/v1.1/sites/pranavcea.wordpress.com/posts/?number=50"
ALLPOST_API_URL = "https://blog.pranavblog.online/allpost"
FRONTEND_URL = "https://blog.pranavblog.online"

CACHE_DIR = "cache"
IMAGE_DIR = os.path.join(CACHE_DIR, "images")
STATIC_CSS_PATH = os.path.join(CACHE_DIR, "local_styles.css")
CACHED_HTML_PATH = os.path.join(CACHE_DIR, "cached_index.html")
MAX_CACHE_SIZE_MB = 300

# Ensure directories exist
os.makedirs(IMAGE_DIR, exist_ok=True)

app = FastAPI(title="To The Point - SSR Engine")

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
    try:
        response = requests.get(image_url, timeout=10)
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
        # Keep only images of the latest 50 posts
        cursor.execute("SELECT featured_image_path, grid_image_path FROM homedata ORDER BY date DESC LIMIT 50")
        recent_images = set()
        for row in cursor.fetchall():
            if row['featured_image_path']: recent_images.add(os.path.basename(row['featured_image_path']))
            if row['grid_image_path']: recent_images.add(os.path.basename(row['grid_image_path']))
        
        for f in os.listdir(IMAGE_DIR):
            if f not in recent_images:
                os.remove(os.path.join(IMAGE_DIR, f))
        conn.close()
        print("Cache cleanup complete.")

# --- Data Sync Logic ---
def sync_data():
    print("Starting data sync...")
    # Fetch Backend Mappings
    try:
        mapping_res = requests.get(ALLPOST_API_URL, timeout=10)
        mappings = {item['original_url']: item['slug'] for item in mapping_res.json()}
    except Exception as e:
        print(f"Failed to fetch mappings: {e}")
        mappings = {}

    # Fetch WP Posts
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
            continue # Post already in DB

        title = post['title']
        original_url = requests.utils.unquote(post['URL'])
        slug = mappings.get(original_url, f"post-{wp_id}")
        date = post['date']
        excerpt = post.get('excerpt', '')
        
        # Get Image
        image_url = post.get('featured_image')
        if not image_url and post.get('content'):
            img_soup = BeautifulSoup(post['content'], 'html.parser')
            img_tag = img_soup.find('img')
            if img_tag: image_url = img_tag.get('src')
        
        feat_path, grid_path = None, None
        if image_url:
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
    print("Data sync and HTML generation complete.")

# --- Server Side Template Generation ---
def generate_static_html():
    if not os.path.exists("index.html"):
        print("index.html not found in root directory!")
        return

    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 1. Remove Tailwind CDN & Inject Local CSS
    for script in soup.find_all('script'):
        if script.get('src') and 'tailwindcss.com' in script.get('src'):
            script.decompose()

    # Create local static CSS to replace user side CDN
    if not os.path.exists(STATIC_CSS_PATH):
        try:
            # Download a compiled robust tailwind core file for local serving
            tailwind_core = requests.get("https://unpkg.com/tailwindcss@2.2.19/dist/tailwind.min.css").text
            with open(STATIC_CSS_PATH, "w", encoding="utf-8") as f:
                f.write(tailwind_core)
        except:
            pass
    
    # Add local CSS link
    css_link = soup.new_tag("link", rel="stylesheet", href="/cache/local_styles.css")
    soup.head.append(css_link)

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
    if slider_track:
        slider_track.clear()
        for post in slider_posts:
            slide_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="w-full flex-shrink-0 relative h-full block animate-fade-in">
                <img src="{post['featured_image_path'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'}" class="w-full h-full object-cover" alt="{post['title']}">
                <div class="slide-overlay absolute inset-0 flex flex-col justify-end p-6 md:p-10" style="background: linear-gradient(to top, rgba(6, 95, 70, 0.95) 0%, rgba(6, 95, 70, 0.4) 60%, transparent 100%);">
                    <span class="bg-green-600/90 backdrop-blur text-white text-[0.65rem] uppercase tracking-widest font-bold px-2 py-1 rounded w-fit mb-3 border border-white/20">Featured</span>
                    <h2 class="text-white text-xl md:text-3xl font-bold leading-tight drop-shadow-lg line-clamp-2 mb-1">{post['title']}</h2>
                    <div class="h-1 w-12 bg-green-400 rounded-full mt-2 mb-1"></div>
                </div>
            </a>
            """
            slider_track.append(BeautifulSoup(slide_html, 'html.parser'))
    
    # Hide skeleton immediately
    skeleton = soup.find(id="sliderSkeleton")
    if skeleton: skeleton.decompose()

    # 4. Populate Grid
    posts_grid = soup.find(id="postsGrid")
    if posts_grid:
        posts_grid.clear()
        for post in grid_posts:
            formatted_date = post['date'].strftime('%m/%d/%Y') if post['date'] else ''
            card_html = f"""
            <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline" style="background: rgba(255, 255, 255, 0.4); backdrop-filter: blur(4px); border: 1px solid rgba(255, 255, 255, 0.5);">
                <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                    <img src="{post['grid_image_path'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'}" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="Thumbnail">
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

    # 5. Inject Clean Client JS for Load More & Slider only
    for script in soup.find_all('script'):
        if script.string and 'fetchAndAppend' in script.string:
            script.decompose()

    clean_js = f"""
    <script>
        let currentSlideIndex = 0;
        const totalSlides = {len(slider_posts)};
        const sliderTrack = document.getElementById('sliderTrack');
        let autoSlideInterval;

        function updateSlider() {{
            const percentage = currentSlideIndex * -100;
            sliderTrack.style.transform = `translateX(${{percentage}}%)`;
        }}

        window.nextSlide = () => {{
            if (totalSlides <= 1) return;
            currentSlideIndex = (currentSlideIndex + 1) % totalSlides;
            updateSlider();
            resetTimer();
        }};

        window.prevSlide = () => {{
            if (totalSlides <= 1) return;
            currentSlideIndex = (currentSlideIndex - 1 + totalSlides) % totalSlides;
            updateSlider();
            resetTimer();
        }};

        function startAutoSlide() {{
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
        
        startAutoSlide();

        // Server-Side Rendered Load More
        async function loadMorePosts() {{
            const btn = document.getElementById('loadMoreBtn');
            const spinner = document.getElementById('btnSpinner');
            btn.disabled = true;
            spinner.classList.remove('hidden');
            
            const currentCount = document.querySelectorAll('#postsGrid > a').length;
            
            try {{
                const res = await fetch(`/more?offset=${{currentCount + 8}}`); // +8 because slider has 8
                const html = await res.text();
                if (html.trim() === "") {{
                    document.getElementById('loadMoreContainer').style.display = 'none';
                }} else {{
                    document.getElementById('postsGrid').insertAdjacentHTML('beforeend', html);
                }}
            }} catch(e) {{
                console.error(e);
            }} finally {{
                btn.disabled = false;
                spinner.classList.add('hidden');
            }}
        }}
    </script>
    """
    soup.body.append(BeautifulSoup(clean_js, 'html.parser'))

    # Save Cached HTML
    with open(CACHED_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(str(soup))


# --- Background Thread ---
def background_task_runner():
    while True:
        try:
            sync_data()
        except Exception as e:
            print(f"Background Task Error: {e}")
        time.sleep(3600) # Sync aur Cache update har 1 ghante mein

# --- FastAPI App Lifecycle & Endpoints ---

@app.on_event("startup")
def startup_event():
    init_db()
    # Start background loop
    thread = threading.Thread(target=background_task_runner, daemon=True)
    thread.start()
    
    # Initial Sync agar HTML cache nahi bana hai
    if not os.path.exists(CACHED_HTML_PATH):
        print("Initial generation running...")
        sync_data()

# Mount cache folder directly
app.mount("/cache", StaticFiles(directory="cache"), name="cache")

@app.get("/", response_class=HTMLResponse)
async def serve_homepage():
    if os.path.exists(CACHED_HTML_PATH):
        with open(CACHED_HTML_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Optimizing & Building Homepage. Please refresh in a minute...</h1>")

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
        html_snippets += f"""
        <a href="{FRONTEND_URL}/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 cursor-pointer group animate-fade-in block text-inherit no-underline" style="background: rgba(255, 255, 255, 0.4); backdrop-filter: blur(4px); border: 1px solid rgba(255, 255, 255, 0.5);">
            <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                <img src="{post['grid_image_path'] or 'https://images.unsplash.com/photo-1500382017468-9049fed747ef'}" loading="lazy" class="w-full h-full object-cover group-hover:scale-110 transition duration-500" alt="Thumbnail">
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
