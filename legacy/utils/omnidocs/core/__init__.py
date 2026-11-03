"""Core utility engines for OmniDocs Studio."""
from .config import AppConfig, CONFIG_FILE
from .arabic import normalize_arabic_and_latin, translate_arabic_digits, has_arabic, prepare_name_tokens
from .fuzzy import compute_name_similarity, levenshtein_ratio, rank_candidates
from .ocr import preprocess_image, detect_and_fix_orientation, detect_and_fix_skew, ocr_image, ocr_mrz_region, detect_faces, has_exif_camera, repair_email_whitespace, setup_tesseract
from .pdf_ops import render_page_thumbnail, build_merged_pdf, rotate_pdf_page, get_document_page_count, is_pdf, is_image, is_docx
from .excel_ops import read_tabular_file, run_sheet_sync, export_sync_workbook, export_matcher_workbook
from .rules import RuleDef, get_default_rules, NECESSITY_LEVELS
from .watcher import DirectoryWatcher, wait_until_stable
from .clipboard import get_clipboard_image, compute_image_hash, copy_to_clipboard, format_spreadsheet_row

