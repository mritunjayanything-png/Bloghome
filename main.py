# main.py - Server-side rendered, SEO Optimized Blog Backend
import os
import time
import json
import asyncio
from io import BytesIO
from urllib.parse import unquote
from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from sqlalchemy import create_engine, Column, Integer, String, Text, JSON, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from PIL import Image
import httpx
from bs4 import BeautifulSoup
from jinja2 import Template

# ==========================================
# 1. Configuration & Constants (Configurations)
# ==========================================
# Aiven DB Connection
DATABASE_URL = "postgresql://avnadmin:AVNS_d9GncXE-Fge9t5p3XlY@pg-7cbbad8-tanyasinghagrawal-62c1.j.aivencloud.com:26734/defaultdb?sslmode=require"

# API Endpoints
BACKEND_ALLPOST_API = "https://blog.pranavblog.online/allpost"
WP_SITE = "pranavcea.wordpress.com"
WP_API_BASE = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE}/posts/"

# Cache Settings
CACHE_DIR = "cache_data"
IMG_CACHE_DIR = os.path.join(CACHE_DIR, "images")
MAX_CACHE_SIZE_MB = 300

# Server State
app = FastAPI(title="To The Point - SSR Backend")
HTML_CACHE = "" # Global variable html cache ke liye
POSTS_CACHE = [] # RAM me posts rakhne ke liye for fast /more api

# Folder setup kar rahe hain
os.makedirs(IMG_CACHE_DIR, exist_ok=True)

# ==========================================
# 2. Database Setup (SQLAlchemy)
# ==========================================
engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Single table with JSON sub-data jaisa aapne manga tha
class HomeData(Base):
    __tablename__ = "homedata"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, index=True)
    original_url = Column(String)
    title = Column(String)
    published_date = Column(String)
    banner_img = Column(String) # 30kb compressed path
    grid_img = Column(String)   # 10kb compressed path
    raw_data = Column(JSON)     # Baki extra details sub-json ke form me

Base.metadata.create_all(bind=engine)

# ==========================================
# 3. Cache & Image Management System
# ==========================================

def enforce_cache_size_limit():
    """300MB se zyada hone par purani images delete karega"""
    total_size = 0
    files_with_time = []
    
    for filename in os.listdir(IMG_CACHE_DIR):
        filepath = os.path.join(IMG_CACHE_DIR, filename)
        if os.path.isfile(filepath):
            size = os.path.getsize(filepath)
            total_size += size
            files_with_time.append((filepath, os.path.getmtime(filepath), size))
            
    total_size_mb = total_size / (1024 * 1024)
    
    # Agar 300MB cross ho gaya toh...
    if total_size_mb > MAX_CACHE_SIZE_MB:
        # Purani files ko pehle delete karne ke liye sort karein
        files_with_time.sort(key=lambda x: x[1])
        
        target_size_mb = MAX_CACHE_SIZE_MB * 0.8 # 80% tak wapas le aao (240MB)
        bytes_to_remove = (total_size_mb - target_size_mb) * 1024 * 1024
        
        removed = 0
        for filepath, _, size in files_with_time:
            try:
                os.remove(filepath)
                removed += size
                if removed >= bytes_to_remove:
                    break
            except Exception as e:
                print(f"Error deleting cache file {filepath}: {e}")

async def compress_and_save_image(url: str, post_id: str, is_banner: bool) -> str:
    """Image ko download karke 30kb (Banner) ya 10kb (Grid) me compress karta hai"""
    target_kb = 30 if is_banner else 10
    target_bytes = target_kb * 1024
    prefix = "banner" if is_banner else "grid"
    filename = f"{prefix}_{post_id}.webp" # Webp format is best for compression
    filepath = os.path.join(IMG_CACHE_DIR, filename)
    
    # Agar pehle se compressed hai, toh wahi return kardo
    if os.path.exists(filepath):
        return f"/cache/images/{filename}"
        
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code != 200:
                return "https://images.unsplash.com/photo-1500382017468-9049fed747ef?ixlib=rb-1.2.1&auto=format&fit=crop&w=1000&q=80"
                
            img = Image.open(BytesIO(resp.content))
            
            # Convert to RGB (in case of PNG with alpha)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
                
            # Resize
            if is_banner:
                img.thumbnail((800, 500), Image.Resampling.LANCZOS)
            else:
                img.thumbnail((300, 300), Image.Resampling.LANCZOS)
                
            # Dynamic compression loop to hit target size
            quality = 90
            while quality > 10:
                output = BytesIO()
                img.save(output, format="WEBP", quality=quality)
                size = output.tell()
                if size <= target_bytes:
                    break
                quality -= 5 # dheere dheere quality kam karein
                
            with open(filepath, "wb") as f:
                f.write(output.getvalue())
                
            enforce_cache_size_limit()
            return f"/cache/images/{filename}"
            
    except Exception as e:
        print(f"Image processing fail for {url}: {e}")
        return url # fallback to original

# ==========================================
# 4. Data Sync & HTML Generation Logic
# ==========================================

async def fetch_and_sync_data():
    """Background task jo DB update karta hai aur HTML render karta hai"""
    global POSTS_CACHE
    print("Background Sync Shuru ho gaya hai...")
    db = SessionLocal()
    try:
        # 1. Get all mappings from backend
        async with httpx.AsyncClient() as client:
            mappings_resp = await client.get(BACKEND_ALLPOST_API, timeout=15.0)
            if mappings_resp.status_code != 200:
                return
            mappings = mappings_resp.json()
            url_to_slug = {unquote(m['original_url']): m['slug'] for m in mappings}

        # 2. Fetch latest posts from WordPress
        async with httpx.AsyncClient() as client:
            wp_resp = await client.get(f"{WP_API_BASE}?number=100", timeout=20.0) # 100 for batch processing
            if wp_resp.status_code != 200:
                return
            wp_data = wp_resp.json()
            
        posts_to_process = wp_data.get('posts', [])
        
        for post in posts_to_process:
            clean_url = unquote(post['URL'])
            slug = url_to_slug.get(clean_url)
            
            if not slug:
                # Naya post, aiven backend API pe register bhi karna chahiye ideally
                continue 
                
            # Check if exists in DB
            db_post = db.query(HomeData).filter(HomeData.slug == slug).first()
            
            # Get Image logic
            img_url = post.get('featured_image')
            if not img_url:
                soup = BeautifulSoup(post['content'], 'html.parser')
                img_tag = soup.find('img')
                img_url = img_tag['src'] if img_tag else "https://images.unsplash.com/photo-1500382017468-9049fed747ef?ixlib=rb-1.2.1&auto=format&fit=crop&w=1000&q=80"
            
            if not db_post:
                # Images compress karke save kar rahe hain (30kb banner, 10kb grid)
                banner_img_path = await compress_and_save_image(img_url, str(post['ID']), True)
                grid_img_path = await compress_and_save_image(img_url, str(post['ID']), False)
                
                new_post = HomeData(
                    slug=slug,
                    original_url=clean_url,
                    title=post['title'],
                    published_date=post['date'],
                    banner_img=banner_img_path,
                    grid_img=grid_img_path,
                    raw_data={"excerpt": post['excerpt'], "id": post['ID']}
                )
                db.add(new_post)
                db.commit()
                print(f"Post cached in DB: {post['title']}")

        # 3. Update RAM Cache for quick access
        all_db_posts = db.query(HomeData).order_by(HomeData.published_date.desc()).all()
        POSTS_CACHE = []
        for p in all_db_posts:
            POSTS_CACHE.append({
                "title": p.title,
                "slug": p.slug,
                "date": p.published_date,
                "banner": p.banner_img,
                "grid": p.grid_img
            })
            
        # 4. Generate the final SSR HTML
        generate_ssr_html()
        print("Sync and HTML Generation Complete!")
        
    except Exception as e:
        print(f"Sync error: {e}")
    finally:
        db.close()

def generate_ssr_html():
    """index.html read karke, JS/Tailwind hatake server-side HTML banayega"""
    global HTML_CACHE, POSTS_CACHE
    
    if not os.path.exists("index.html"):
        print("index.html root me nahi mila!")
        return

    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    
    # CDN Tailwind remove kar diya taki dependency na rahe
    for script in soup.find_all('script'):
        if 'cdn.tailwindcss.com' in script.get('src', ''):
            script.decompose()

    # Custom Core CSS embed karna (Taki bina CDN sab dikhe)
    core_css = """
    <style>
        /* Custom Generated SSR CSS - No external tailwind needed */
        body { margin: 0; background: linear-gradient(135deg, #f0fdf4 0%, #dcfce7 50%, #bbf7d0 100%); color: #1f2937; }
        .max-w-5xl { max-width: 64rem; margin-left: auto; margin-right: auto; }
        .px-6 { padding-left: 1.5rem; padding-right: 1.5rem; }
        .py-3 { padding-top: 0.75rem; padding-bottom: 0.75rem; }
        .flex { display: flex; } .flex-col { display: flex; flex-direction: column; }
        .justify-between { justify-content: space-between; } .items-center { align-items: center; }
        .gap-3 { gap: 0.75rem; } .gap-4 { gap: 1rem; } .gap-6 { gap: 1.5rem; }
        .grid { display: grid; } .grid-cols-1 { grid-template-columns: repeat(1, minmax(0, 1fr)); }
        @media (min-width: 768px) { .md\\:grid-cols-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        .fixed { position: fixed; } .w-full { width: 100%; } .top-0 { top: 0; } .z-50 { z-index: 50; }
        .pt-24 { padding-top: 6rem; } .pb-12 { padding-bottom: 3rem; }
        .rounded-3xl { border-radius: 1.5rem; } .rounded-2xl { border-radius: 1rem; }
        .overflow-hidden { overflow: hidden; } .relative { position: relative; }
        .h-64 { height: 16rem; } @media (min-width: 768px) { .md\\:h-\\[400px\\] { height: 400px; } }
        .object-cover { object-fit: cover; width: 100%; height: 100%; }
        .absolute { position: absolute; } .inset-0 { top: 0; right: 0; bottom: 0; left: 0; }
        .text-white { color: #fff; } .font-bold { font-weight: 700; }
        .text-green-900 { color: #14532d; } .text-green-700 { color: #15803d; }
        .mb-2 { margin-bottom: 0.5rem; } .mb-4 { margin-bottom: 1rem; } .mb-6 { margin-bottom: 1.5rem; }
        a { text-decoration: none; color: inherit; }
        .ssr-slide { display: none; width: 100%; height: 100%; }
        .ssr-slide.active { display: block; } /* CSS fallback for slider */
    </style>
    """
    soup.head.append(BeautifulSoup(core_css, 'html.parser'))

    # SSR Render logic - Jinja syntax lagayenge BeautifulSoup parsing ke sath
    featured_posts = POSTS_CACHE[:8]
    grid_posts = POSTS_CACHE[8:20]

    # --- 1. Populate Slider ---
    slider_track = soup.find(id="sliderTrack")
    if slider_track:
        slider_track.clear()
        for idx, post in enumerate(featured_posts):
            date_str = post['date'].split('T')[0]
            # Server rendered anchor tags
            slide_html = f"""
            <a href="https://blog.pranavblog.online/{post['slug']}" class="w-full flex-shrink-0 relative h-full ssr-slide {'active' if idx==0 else 'hidden'}">
                <img src="{post['banner']}" class="w-full h-full object-cover" alt="{post['title']}">
                <div class="slide-overlay absolute inset-0 flex flex-col justify-end p-6 md:p-10">
                    <span class="bg-green-600/90 backdrop-blur text-white text-[0.65rem] uppercase tracking-widest font-bold px-2 py-1 rounded w-fit mb-3">Featured</span>
                    <h2 class="text-white text-xl md:text-3xl font-bold leading-tight drop-shadow-lg line-clamp-2 mb-1">{post['title']}</h2>
                </div>
            </a>
            """
            slider_track.append(BeautifulSoup(slide_html, 'html.parser'))
            
    # Skeleton slider remove
    skeleton = soup.find(id="sliderSkeleton")
    if skeleton: skeleton.decompose()

    # --- 2. Populate Grid ---
    posts_grid = soup.find(id="postsGrid")
    if posts_grid:
        posts_grid.clear()
        for post in grid_posts:
            date_str = post['date'].split('T')[0]
            grid_html = f"""
            <a href="https://blog.pranavblog.online/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 block text-inherit no-underline">
                <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                    <img src="{post['grid']}" loading="lazy" class="w-full h-full object-cover" alt="{post['title']}">
                </div>
                <div class="flex flex-col justify-center flex-1 min-w-0">
                    <h4 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2">{post['title']}</h4>
                    <div class="flex items-center gap-2 text-xs text-green-700/80 mb-2">
                        <span>{date_str}</span>
                    </div>
                </div>
            </a>
            """
            posts_grid.append(BeautifulSoup(grid_html, 'html.parser'))

    # Load More Button logic adjustment
    load_more = soup.find(id="loadMoreBtn")
    if load_more:
        # JS load more functionality modify kar diya client ke liye
        load_more['onclick'] = "fetchMoreSSR()"

    # Replace specific JS block for SSR compatibility
    script_tag = """
    <script>
        // JS is strictly for client-side interactions now (slider/load more)
        let ssrSlides = document.querySelectorAll('.ssr-slide');
        let currentSsrSlide = 0;
        
        function updateSsrSlider() {
            ssrSlides.forEach((s, i) => {
                if(i === currentSsrSlide) { s.classList.remove('hidden'); s.classList.add('active'); }
                else { s.classList.add('hidden'); s.classList.remove('active'); }
            });
        }
        
        window.nextSlide = () => { currentSsrSlide = (currentSsrSlide + 1) % ssrSlides.length; updateSsrSlider(); };
        window.prevSlide = () => { currentSsrSlide = (currentSsrSlide - 1 + ssrSlides.length) % ssrSlides.length; updateSsrSlider(); };
        
        setInterval(window.nextSlide, 4000); // Auto slide fallback

        // Load more SSR handling
        let currentOffset = 20; // 8 banner + 12 grid
        async function fetchMoreSSR() {
            const btn = document.getElementById('loadMoreBtn');
            const grid = document.getElementById('postsGrid');
            btn.innerHTML = "Loading...";
            
            try {
                let res = await fetch('/more?offset=' + currentOffset);
                let html = await res.text();
                if(html.trim().length > 0) {
                    grid.insertAdjacentHTML('beforeend', html);
                    currentOffset += 12;
                    btn.innerHTML = "View More Articles";
                } else {
                    btn.parentElement.style.display = 'none'; // No more posts
                }
            } catch(e) { console.error(e); btn.innerHTML = "Error!"; }
        }
    </script>
    """
    
    # Inject our new minimal client script
    soup.body.append(BeautifulSoup(script_tag, 'html.parser'))

    # Global HTML cache update
    HTML_CACHE = str(soup)

# ==========================================
# 5. FastAPI App Lifecycle & Endpoints
# ==========================================

@app.on_event("startup")
async def startup_event():
    """Server start hote hi cache generate karega background me"""
    # Pehle DB se check karega ki data hai kya
    db = SessionLocal()
    posts = db.query(HomeData).order_by(HomeData.published_date.desc()).all()
    if posts:
        global POSTS_CACHE
        for p in posts:
            POSTS_CACHE.append({
                "title": p.title, "slug": p.slug, "date": p.published_date,
                "banner": p.banner_img, "grid": p.grid_img
            })
        generate_ssr_html() # Instant HTML generate from DB
    db.close()
    
    # Aur naye data ke liye sync run kar dega
    asyncio.create_task(fetch_and_sync_data())


@app.get("/", response_class=HTMLResponse)
async def serve_homepage():
    """User ko seedha cached HTML serve karega. 0 JS rendering time."""
    if HTML_CACHE:
        return HTML_CACHE
    return "<h1>Loading... Server is warming up and caching the data. Please refresh in 10 seconds.</h1>"


@app.get("/more", response_class=HTMLResponse)
async def load_more_posts(offset: int = 20, limit: int = 12):
    """HTML snippets return karega load more button dabane par. Fully SEO."""
    global POSTS_CACHE
    posts_to_return = POSTS_CACHE[offset : offset + limit]
    
    html_snippets = ""
    for post in posts_to_return:
        date_str = post['date'].split('T')[0]
        html_snippets += f"""
        <a href="https://blog.pranavblog.online/{post['slug']}" class="glass-list-item rounded-2xl p-4 flex gap-4 block text-inherit no-underline">
            <div class="w-24 h-24 md:w-32 md:h-32 flex-shrink-0 rounded-xl overflow-hidden relative shadow-sm">
                <img src="{post['grid']}" loading="lazy" class="w-full h-full object-cover" alt="{post['title']}">
            </div>
            <div class="flex flex-col justify-center flex-1 min-w-0">
                <h4 class="font-bold text-green-900 leading-snug mb-2 text-sm md:text-base line-clamp-2">{post['title']}</h4>
                <div class="flex items-center gap-2 text-xs text-green-700/80 mb-2">
                    <span>{date_str}</span>
                </div>
            </div>
        </a>
        """
    return html_snippets


@app.get("/cache/images/{filename}")
async def get_cached_image(filename: str):
    """Local server se compressed images serve karega"""
    filepath = os.path.join(IMG_CACHE_DIR, filename)
    if os.path.exists(filepath):
        return FileResponse(filepath)
    return FileResponse(os.path.join(IMG_CACHE_DIR, "fallback.jpg")) # Keep a dummy file

if __name__ == "__main__":
    import uvicorn
    # Application run karne ke liye
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
