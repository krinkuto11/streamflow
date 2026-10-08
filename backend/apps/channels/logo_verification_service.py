"""
Logo Verification Service

Cross-Stream Consensus Architecture for resilient, computer-vision-based Logo Verification.
Implements Scale-Invariant Edge Matching to ensure the right channel is broadcast.
"""

import os
import cv2
import numpy as np
import imutils
import logging
import threading
from pathlib import Path

from apps.channels.logo_cache import download_and_cache_logo, find_cached_logo

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get('CONFIG_DIR', '/app/data'))
LOGOS_CACHE_DIR = CONFIG_DIR / 'logos_cache'

# Global semaphore to limit concurrent template matching operations (CPU-heavy)
POOL_SEMAPHORE = threading.Semaphore(2)

def get_cached_logo_path(logo_id: int) -> str | None:
    """Finds the local cached path for a given logo ID, downloading it if necessary."""
    if not logo_id:
        return None
        
    cached_path = find_cached_logo(LOGOS_CACHE_DIR, logo_id)
    if cached_path is not None:
        return str(cached_path)
            
    # Logo not cached, try to download it via UDI Manager logic
    try:
        from apps.udi.manager import get_udi_manager
        from apps.config.dispatcharr_config import get_dispatcharr_config
        
        udi = get_udi_manager()
        logo = udi.get_logo_by_id(logo_id)
        if not logo:
            return None
            
        dispatcharr_base_url = get_dispatcharr_config().get_base_url() or os.getenv("DISPATCHARR_BASE_URL", "")
        logo_url = logo.get('cache_url') or logo.get('url')
        
        if not logo_url:
            return None
            
        logger.debug("Downloading missing logo %s", logo_id)
        cached_path = download_and_cache_logo(LOGOS_CACHE_DIR, logo_id, logo_url, dispatcharr_base_url)
        return str(cached_path)
    except Exception as e:
        logger.error(f"Failed to fetch cache missing logo {logo_id}: {e}")
        return None

def process_roi_matching(scene_roi: np.ndarray, template_gray: np.ndarray) -> float:
    """
    Process the Region of Interest for logo matching.
    
    1. Edge Density Sanity Check (skip black screens/whip-pans).
    2. Primary: Direct Grayscale Template Matching (better for translucent/solid logos).
    3. Fallback: Edge Matching (better for stark contrast overlays).
    
    Returns: (highest_score, best_scale), or (-1.0, 0.0) if skipped.
    """
    # Contrast Enhancement: Stretch the ROI to improve semi-translucent logo visibility
    scene_roi = cv2.normalize(scene_roi, None, 0, 255, cv2.NORM_MINMAX)
    
    # Apply a heavier 5x5 blur to wash out high-frequency crowd noise
    scene_blurred = cv2.GaussianBlur(scene_roi, (5, 5), 0)
    # Slightly stricter Canny to ignore background artifacting
    scene_edges = cv2.Canny(scene_blurred, 20, 60)
    
    # Pillar 1 - Edge Density Sanity Check
    edge_density = cv2.countNonZero(scene_edges)
    if edge_density < 500:
        return -1.0, 0.0
        
    highest_score = 0.0
    best_scale = 0.0
    
    with POOL_SEMAPHORE:
        # Primary: Direct Grayscale Matching
        for scale in np.linspace(0.2, 1.5, 14):
            width = int(template_gray.shape[1] * scale)
            if width <= 0: continue
            resized_template = imutils.resize(template_gray, width=width, inter=cv2.INTER_AREA)
            if resized_template.shape[0] > scene_roi.shape[0] or resized_template.shape[1] > scene_roi.shape[1]:
                continue
            result = cv2.matchTemplate(scene_roi, resized_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            
            if max_val > highest_score:
                highest_score = max_val
                best_scale = scale
                
        if highest_score >= 0.50:
            return highest_score, best_scale
            
        # Fallback: Edge Matching
        for scale in np.linspace(0.2, 1.5, 14):
            width = int(template_gray.shape[1] * scale)
            if width <= 0: continue
            resized_template = imutils.resize(template_gray, width=width, inter=cv2.INTER_AREA)
            if resized_template.shape[0] > scene_blurred.shape[0] or resized_template.shape[1] > scene_blurred.shape[1]:
                continue
            template_edges = cv2.Canny(resized_template, 50, 200)
            result = cv2.matchTemplate(scene_edges, template_edges, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)
            
            if max_val > highest_score:
                highest_score = max_val
                best_scale = scale
            
    return highest_score, best_scale

def verify_logo(screenshot_path: str, logo_id: int) -> str:
    """
    Verify if the channel's official EPG logo exists in the given screenshot.
    
    Implements Scale-Invariant Edge Matching:
    - Alpha Channel Extraction
    - Auto-Crop
    - Hypersensitive Scene Edges
    - Multi-Scale Crisp Matching
    - Multi-Corner Fallback (Pillar 2)
    
    Returns:
        "SUCCESS", "FAILED", or "SKIPPED"
    """
    
    # 0. Resolve screenshot path if it's relative
    if screenshot_path and not screenshot_path.startswith('/'):
        screenshot_path = os.path.join(CONFIG_DIR, screenshot_path)
        
    if not screenshot_path or not os.path.exists(screenshot_path):
        logger.warning(f"Screenshot path invalid or missing: {screenshot_path}")
        return "SKIPPED"
        
    # 1. Resolve local cached logo path
    logo_path = get_cached_logo_path(logo_id)
    if not logo_path:
        logger.debug(f"Cached logo not found for ID: {logo_id}")
        return "SKIPPED"
        
    try:
        # Load screenshot
        screenshot = cv2.imread(screenshot_path, cv2.IMREAD_COLOR)
        if screenshot is None:
            logger.warning(f"Failed to load screenshot: {screenshot_path}")
            return "SKIPPED"
            
        # Normalize canvas to 720p width
        screenshot = imutils.resize(screenshot, width=1280)
            
        # Load template logo (with alpha channel if exists)
        template_raw = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
        if template_raw is None:
            logger.warning(f"Failed to load template logo: {logo_path}")
            return "SKIPPED"
            
        # Alpha Channel Extraction & Compositing
        if len(template_raw.shape) == 3 and template_raw.shape[2] == 4:
            bgr = template_raw[:, :, 0:3]
            alpha = template_raw[:, :, 3]
            
            # Preserve internal logo geometry by compositing to grayscale 
            # instead of treating the alpha mask as the entire image
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            template_gray = cv2.bitwise_and(gray, gray, mask=alpha)
            
            # Ensure crop processes against the alpha mask to capture dark bounds too
            crop_target = alpha
        else:
            template_gray = cv2.cvtColor(template_raw, cv2.COLOR_BGR2GRAY)
            crop_target = template_gray
            
        # Bound the true visual extent, avoiding microscopic dust
        _, thresh = cv2.threshold(crop_target, 50, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid_points = [c for c in contours if cv2.contourArea(c) > 10]
        
        if not valid_points:
            logger.debug("Template cropped to zero area after dust filter.")
            return "SKIPPED"
            
        # Crop the grayscale image to the bounds
        x, y, w, h = cv2.boundingRect(np.vstack(valid_points))
        template_cropped = template_gray[y:y+h, x:x+w]
            
        # Normalize template width to 200px so scale loop [0.5, 1.5] blankets 100px - 300px
        template_cropped = imutils.resize(template_cropped, width=200)
            
        # Hypersensitive Scene Edges: ROI extraction
        h, w = screenshot.shape[:2]
        
        # Top-Right quadrant (0-35% height, 65-100% width)
        tr_roi = screenshot[0:int(h * 0.35), int(w * 0.65):w]
        # Convert ROI to grayscale as well just in case for blurring
        tr_roi_gray = cv2.cvtColor(tr_roi, cv2.COLOR_BGR2GRAY)
        
        score_tr, scale_tr = process_roi_matching(tr_roi_gray, template_cropped)
        if score_tr == -1.0:
            return "SKIPPED"
            
        if score_tr >= 0.45:
            logger.debug(f"Logo match SUCCESS in Top-Right quadrant. Score: {score_tr:.2f}, Scale: {scale_tr:.2f}")
            return "SUCCESS"
            
        # Pillar 2 - Multi-Corner Fallback
        # If Top-Right fails (score < 0.45), immediately repeat for Top-Left
        tl_roi = screenshot[0:int(h * 0.35), 0:int(w * 0.35)]
        tl_roi_gray = cv2.cvtColor(tl_roi, cv2.COLOR_BGR2GRAY)
        
        score_tl, scale_tl = process_roi_matching(tl_roi_gray, template_cropped)
        if score_tl == -1.0:
            return "SKIPPED"
            
        if score_tl >= 0.45:
            logger.debug(f"Logo match SUCCESS in Top-Left quadrant. Score: {score_tl:.2f}, Scale: {scale_tl:.2f}")
            return "SUCCESS"
            
        logger.debug(f"Logo match FAILED across multi-corner ROIs. TR: {score_tr:.2f} (scale {scale_tr:.2f}), TL: {score_tl:.2f} (scale {scale_tl:.2f})")
        return "FAILED"
        
    except Exception as e:
        logger.error(f"Exception during verify_logo: {e}", exc_info=True)
        return "SKIPPED"
