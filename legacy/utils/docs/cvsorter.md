# CVSorter — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**CVSorter** is an intelligent, automated desktop application designed for HR professionals, recruiters, and document managers. It monitors an incoming directory ("CV_Inbox"), processes arriving resumes/CVs across diverse file formats (PDF, DOCX, DOC, PPTX, Images, RTF, TXT), automatically performs OCR on scanned pages or image documents, extracts structured applicant metadata (name, email, phone, job title, company, and custom regex/keyword fields), reconciles names against a known candidate database, renders a verification and review GUI with visual document previews, sanitizes and renames files based on dynamic templates, moves or copies them to organized target folders, and copies structured row data (e.g. for Excel/CRM) to the system clipboard.

---

## 2. Exhaustive Feature Inventory

### 2.1 File Ingestion & Directory Watching (`cvsorter/watcher.py`)
- **Automated Directory Monitoring**: Uses Python's `watchdog` library (`FolderWatcher`) to listen for newly created or modified files in real-time.
- **File Stability / Quiet-Period Detection**:
  - Implements `wait_until_stable(path, quiet_seconds, timeout)`.
  - Probes file size, modification timestamp, and read permissions repeatedly until the file remains unchanged for a configurable window (`stable_seconds`, default: 1.5s).
  - Prevents race conditions and partial-read errors while files are actively downloading or copying across local/network storage.
- **Temporary & Lock File Exclusion**: Automatically ignores transient files matching prefixes (`~$`, `.`) or suffixes (`.tmp`, `.crdownload`, `.part`, `.partial`, `.download`).
- **Startup Processing Mode**: Configurable `process_existing` option to scan and process any files already present in the inbox at application startup.
- **Multi-threaded Background Processing**: Asynchronous worker execution via `ThreadPoolExecutor` so watching and OCR never block the Tkinter user interface.

### 2.2 Multi-Format Document Reading & Conversion (`cvsorter/readers.py`)
- **Supported Formats**: `.pdf`, `.docx`, `.doc`, `.pptx`, `.ppt`, `.txt`, `.rtf`, `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp`, `.gif`.
- **Intelligent Hybrid PDF Extraction**:
  - Leverages `pdfplumber` or `PyMuPDF` (`fitz`) for native text layer extraction.
  - Page-by-page character inspection: If a PDF page contains fewer than `ocr_min_chars` (default: 80 characters) and OCR is enabled, that individual page is rendered to a high-resolution raster image (at `ocr_dpi`, default: 200 DPI) and passed through Tesseract OCR.
- **Native Microsoft Office Ingestion**:
  - `.docx`: Extracts text from all body paragraphs, header paragraphs, and table cell matrices using `python-docx`.
  - `.pptx`: Traverses all slide decks, shape trees, tables, and text frames using `python-pptx`.
- **Legacy Office & Formats via Headless Conversion**:
  - `.doc`, `.ppt`, `.rtf`, `.odt`, `.odp`: Employs headless LibreOffice (`soffice --headless --convert-to pdf`) in isolated temporary directories to generate an intermediate PDF, which is then parsed by the PDF engine.
- **Multi-frame & Single-frame Image Reading**:
  - Handles multi-page TIFFs and animated GIFs via `PIL.ImageSequence`.
  - Runs full OCR across every frame and aggregates extracted text.
- **Plain Text Fallback**: Robust decoding pipeline attempting `utf-8`, `utf-8-sig`, `cp1252`, and `latin-1`.

### 2.3 Optical Character Recognition (OCR) Engine (`cvsorter/ocr.py`)
- **Tesseract Integration**: Directly interfaces with `pytesseract` with configurable binary path (`tesseract_cmd`).
- **Image Preprocessing**:
  - Converts images to grayscale (`L`).
  - Auto-upscaling: Resizes images with width < 1600px using high-quality Lanczos resampling (`Image.LANCZOS`) to enhance OCR accuracy on low-resolution scans.
  - Contrast optimization via `ImageOps.autocontrast`.
- **Multilingual Support**: Supports single or multi-language models (e.g. `eng+ara+fra+deu`) passed directly to Tesseract.
- **Diagnostics**: Built-in verification routine (`check_tesseract()`) that checks version and path validity from the Settings UI.

### 2.4 Extraction Pipeline & Heuristics (`cvsorter/extractor.py`)
- **Full Name Extraction (`ex_name`)**:
  - Scans top 15 non-empty lines of the document.
  - Filters out lines longer than 40 characters or containing digits/email symbols (`@`).
  - Disqualifies header and structural stopwords (`DEFAULT_NAME_STOPWORDS`: *resume, curriculum vitae, profile, summary, objective, contact, experience, education, skills, email, phone, linkedin, etc.*).
  - Enforces word count between 2 and 4 words.
  - Validates alphabetic density (> 90% letters or spaces).
  - Formats ASCII names to Title Case while preserving native script casing (e.g. Arabic).
- **Email Extraction (`ex_email`)**:
  - Robust regex pattern: `[\w.+-]+@[\w-]+(?:\.[\w-]+)+`.
  - Repairs spaces introduced by OCR around `@` symbols.
  - Collects all valid addresses; ranks the first as primary and stores alternates in `{email_all}`.
- **Phone Number Extraction (`ex_phone`)**:
  - Multi-pattern regex detecting international, formatted, dashed, bracketed, or dot-separated telephone numbers.
  - Normalizes numbers: strips formatting symbols, trims leading international double zeros (`00` -> `+`), validates digit length (between 9 and 15 digits).
- **Job Title Detection (`ex_job_title`)**:
  - Evaluates text against `DEFAULT_JOB_TITLE_KEYWORDS` (e.g. *engineer, developer, architect, manager, specialist, accountant, technician, etc.*).
  - Employs word-boundary regexes with optional fuzzy typo tolerance (`fuzzy_job_titles`).
  - Prioritizes matches located near the document header.
- **Company Detection (`ex_company`)**:
  - Detects corporate suffixes (`DEFAULT_COMPANY_SUFFIXES`: *Inc, LLC, Ltd, Corp, GmbH, PLC, Technologies, Solutions, Bank, etc.*).
  - **Company Snap**: Snaps detected company variations to a canonical company name from a user-provided `known_companies` list using fuzzy similarity.
- **Extensible Custom Fields (`CustomField`)**:
  - Allows unlimited user-defined fields configured as either **Regex** or **Keywords**.
  - Configurable properties: `name`, `kind`, `pattern`, `ignore_case`, `multiple` (return first or all joined), `fuzzy` (typo tolerance).
  - Automatically derives lower_snake_case key for templates (e.g., "Years of Experience" -> `years_of_experience`).

### 2.5 Fuzzy Matching & Name Registry (`cvsorter/fuzzy.py`, `cvsorter/registry.py`)
- **Fast Similarity Algorithm**:
  - Utilizes `rapidfuzz.fuzz.ratio` when available, with a graceful standard library `difflib.SequenceMatcher` fallback.
  - Performance optimizations: Short strings (< 4 chars) require exact match; enforces matching initial letters; restricts length variance to ~25%.
- **Live Candidate Registry (`NameRegistry`)**:
  - Re-reads an external `.txt` (one name per line) or `.csv` (header detection: *name, full name, candidate*) whenever the file is updated on disk.
  - Compares the extracted candidate name against the registry:
    - **Exact Match** (score >= 0.999): Red alert indicator (candidate already exists).
    - **Probable Match** (score >= `name_match_threshold`, default 85%): Orange warning indicator.
    - **New Candidate** (score < threshold): Green confirmation indicator.
- **Auto-Growth**: Option to automatically append approved candidate names to the external registry upon saving.

### 2.6 Visual Document Preview (`cvsorter/preview.py`)
- **Threaded Rasterizer**: Renders visual preview of original document pages in the background without freezing the GUI.
- **Multi-Page Browsing**: Configurable preview limit (`preview_pages`, default: 6 pages; `preview_width`, default: 760px).
- **Office Document Visuals**: Automatically converts Word, PowerPoint, RTF, or OpenDocument files to PDF via LibreOffice to generate page thumbnails.
- **Dual Tab Mode**: Allows switching between rendered image pages and the raw OCR/extracted plain text.

### 2.7 Interactive Verification Dialog (`cvsorter/gui/review_dialog.py`)
- **Side-by-Side Verification Layout**:
  - Left pane: Form fields for Name, Job Title, Company, Phone, Email, and all Custom Fields with instant live editing.
  - Live Duplicate/Registry Status banner with color-coded alerts (Green/Orange/Red).
  - Live Filename Preview: Dynamically shows how the file will be renamed based on the current field values.
  - Generated URLs: Real-time preview of clickable web/cloud links.
  - Quick-copy buttons for single fields and configured multi-field clipboard templates.
  - Right pane: Visual page-by-page document viewer + raw extracted text tab.
- **Decision Actions**:
  - **Save & Move / Copy**: Confirms the edits, performs the file rename/transfer, logs history, updates registry, and optionally copies formatted data to the clipboard.
  - **Send to Duplicates**: Diverts the file to a dedicated duplicate quarantine folder.
  - **Skip**: Leaves the file intact in the inbox without changes.
- **Auto-Confirm Mode**: Option to bypass the dialog entirely and automatically organize files if no duplicate warnings are triggered.

### 2.8 File Operations & Templating (`cvsorter/fileops.py`)
- **Dynamic File Naming Template**:
  - Configurable format using placeholders: `{name}`, `{job_title}`, `{company}`, `{phone}`, `{email}`, `{date}`, `{original}`, `{stem}`, `{ext}`, plus any custom field keys.
- **Filename Sanitization**: Automatically scrubs reserved/illegal filesystem characters (`[<>:"/\\|?*\x00-\x1f]`), collapses whitespace, and trims trailing dots/spaces up to a safe 150-character limit.
- **File Actions**: Configurable `move` or `copy`.
- **Collision Policies**:
  - `number`: Appends progressive numbering (`File (1).pdf`, `File (2).pdf`).
  - `overwrite`: Replaces existing target.
  - `skip`: Aborts saving that file.
- **Dedicated Duplicates Directory**: Isolates flagged files into a subfolder or custom path.

### 2.9 Clipboard & External Links (`cvsorter/clipboard.py`, `cvsorter/links.py`)
- **Customizable Clipboard Formats**:
  - Pre-packaged templates:
    - *Excel row*: `{name}\t{job_title}\t{phone}\t{email}\t{company}`
    - *One line*: `{name} | {job_title} | {phone} | {email}`
    - *Contact card*: `{name}\n{job_title} - {company}\n{phone}\n{email}`
  - Supports tabs (`\t`) and newlines (`\n`).
  - Column alignment safety: Empty fields remain blank tabs so spreadsheet columns never shift.
  - Optional `clipboard_auto` trigger on every completed document.
- **Cloud/CRM URL Link Templates**:
  - Builds dynamic URLs (e.g. `https://mycompany.sharepoint.com/recruitment/{filename}`).
  - Automatically URL-encodes values while providing `_raw` options.

### 2.10 Audit Trail & Logging (`cvsorter/history.py`)
- **Append-Only CSV Log (`cv_history.csv`)**:
  - Columns: `timestamp`, `status` (*saved*, *duplicate*, *recorded*), `original_file`, `saved_as`, plus all extracted metadata fields.
- **Internal Rotating App Log (`cv_sorter.log`)**: Detailed system logging for troubleshooting.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```python
# CVSORTER PIPELINE PSEUDO-SCRIPT

def run_cvsorter_daemon():
    settings = load_settings("config.json")
    registry = NameRegistry(settings.names_list_path, settings.name_match_threshold)
    watcher = FolderWatcher(settings.watch_folder, quiet_seconds=settings.stable_seconds)
    
    # 1. Listen for new files
    for file_path in watcher.events():
        if not wait_until_stable(file_path, settings.stable_seconds):
            continue
            
        # 2. Read and extract raw text
        read_result = read_document(file_path, settings)
        # If PDF has < ocr_min_chars or file is image:
        #    read_result.text = ocr_image(img, lang=settings.ocr_languages)
        
        # 3. Analyze and extract fields
        fields = {}
        fields["name"] = extract_name(read_result.text, stopwords=settings.name_stopwords)
        fields["email"] = extract_email(read_result.text)
        fields["phone"] = extract_phone(read_result.text)
        fields["job_title"] = extract_job_title(read_result.text, keywords=settings.job_title_keywords)
        fields["company"] = extract_company(read_result.text, suffixes=settings.company_suffixes, snap_list=settings.known_companies)
        
        for custom_field in settings.custom_fields:
            fields[custom_field.key] = extract_custom(read_result.text, custom_field)
            
        # 4. Check registry for duplicates
        match = registry.match(fields["name"])
        # match.score >= 0.999 -> EXACT DUPLICATE
        # match.score >= threshold -> PROBABLE MATCH
        # match.score < threshold  -> NEW APPLICANT
        
        # 5. Review & User Interaction
        if settings.auto_confirm and match.score < settings.name_match_threshold:
            decision = Decision.SAVE
        else:
            decision, updated_fields = ReviewDialog.show_modal(
                file=file_path,
                preview=render_preview(file_path),
                extracted_fields=fields,
                match_status=match,
                output_name_preview=render_filename(settings.filename_template, fields)
            )
            fields = updated_fields
            
        # 6. Execute File Actions
        if decision == Decision.SAVE:
            target_name = sanitize(render_filename(settings.filename_template, fields))
            destination = resolve_collision(settings.output_folder / target_name, policy=settings.duplicate_policy)
            transfer(file_path, destination, action=settings.file_action)
            
            if settings.names_auto_add:
                registry.append(fields["name"])
                
            if settings.clipboard_auto:
                copy_to_clipboard(render_clipboard(settings.clipboard_default, fields))
                
            history.append(settings, original=file_path, saved=destination, fields=fields, status="saved")
            
        elif decision == Decision.DUPLICATE:
            destination = settings.duplicates_folder / file_path.name
            transfer(file_path, destination, action=settings.file_action)
            history.append(settings, original=file_path, saved=destination, fields=fields, status="duplicate")
            
        elif decision == Decision.SKIP:
            # Leave file in inbox
            pass
```

---

## 4. Configuration & Default Reference Table

| Setting Key | Type | Default Value | Description |
|---|---|---|---|
| `watch_folder` | string | `~/CV_Inbox` | Source folder to monitor for new CVs |
| `output_folder` | string | `~/CV_Sorted` | Target folder for organized CVs |
| `filename_template` | string | `{name}` | Dynamic template for renaming files |
| `duplicate_policy` | enum | `number` | `number` (append `(1)`), `overwrite`, or `skip` |
| `file_action` | enum | `move` | `move` or `copy` files from watch folder |
| `stable_seconds` | float | `1.5` | File modification inactivity wait time |
| `ocr_enabled` | boolean | `True` | Whether to perform OCR on scanned pages |
| `tesseract_cmd` | string | `""` | Path to `tesseract.exe` (empty uses PATH) |
| `ocr_languages` | string | `eng` | Tesseract language codes (e.g. `eng+ara`) |
| `ocr_dpi` | int | `200` | Resolution for rasterizing PDF pages for OCR |
| `ocr_min_chars` | int | `80` | Threshold below which PDF pages trigger OCR |
| `fuzzy_threshold` | int | `80` | Minimum similarity % for keyword matching |
| `name_match_threshold`| int | `85` | Similarity % warning for existing registry names |
| `names_auto_add` | boolean | `True` | Append newly approved names to candidate registry |
| `clipboard_auto` | boolean | `False` | Auto-copy default clipboard template upon saving |
| `clipboard_default` | string | `Excel row` | Active clipboard formatting preset |
| `preview_pages` | int | `6` | Maximum visual preview pages to render |
| `preview_width` | int | `760` | Pixel width for preview page rendering |
| `libreoffice_cmd` | string | `""` | Path to `soffice` for legacy office files |

