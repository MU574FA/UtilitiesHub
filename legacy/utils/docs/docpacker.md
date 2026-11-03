# DocPacker — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**DocPacker** is an automated document validation, organization, compliance-checking, and compilation suite. It processes applicant onboarding documentation (passports, photos, bank confirmation letters, medical reports, and certificates), verifies rigorous requirement rules (including MRZ checksums, facial detection, document recency, EXIF camera checks, and name consistency), provides manual checklist confirmation, and compiles all approved items into a single, professional PDF complete with an automated cover page, verification checklist, and bookmarks.

---

## 2. Exhaustive Feature Inventory

### 2.1 File Ingestion & Multi-Source Support
- **Drag-and-Drop Ingestion**: Native Windows/desktop drag-and-drop powered by `TkinterDnD2` (with graceful fallback to manual browse dialogs when missing).
- **Supported File Formats**: PDF (`.pdf`), JPEG (`.jpg`, `.jpeg`), PNG (`.png`), TIFF (`.tif`, `.tiff`), BMP (`.bmp`), WebP (`.webp`).
- **Folder Watcher Daemon**: Configurable background polling monitor (`watch_folder`, `watch_interval`) that continuously tracks arriving applicant files.
- **Auto-Scan on New**: Option (`auto_scan_on_new`) to trigger OCR and requirement evaluation immediately upon detecting new files.
- **Batch Processing Mode (`batch_run`)**:
  - Traverses nested applicant directories where each sub-folder represents an individual candidate.
  - Sequentially applies compliance checks, generates individual cover pages, and outputs combined applicant packages into designated target folders.

### 2.2 Rule-Based Requirements Engine (`DEFAULT_REQS`)
- **Configurable Document Requirements**:
  1. **Passport Copy**:
     - Detection: Keywords (*passport, passeport, date of birth, nationality, surname, given names*), MRZ regex (`P<[A-Z]{3}`, `[A-Z0-9<]{20,}<<`).
     - Validation Checks:
       - `surname_filled`: Verifies the applicant surname is entered in the profile.
       - `passport_surname`: Confirms the surname appears in the document text/MRZ.
       - `surname_matches`: Verifies strict or fuzzy identity consistency across all documents.
       - `no_exif_camera`: Inspects image EXIF metadata; fails if taken with a smartphone camera (enforcing flatbed scanner copies).
       - `min_res`: Verifies minimum resolution threshold (e.g. 1000px).
       - `regex` MRZ detection (`<<`).
       - `mrz_valid`: Validates ICAO 9303 modulus check digits.
       - `passport_expiry`: Ensures passport expiry date is at least 6 months after the applicant's joining date.
  2. **Half-Length Photo**:
     - Detection: Image file types with low-text bonus scoring.
     - Validation Checks:
       - `portrait`: Validates portrait aspect ratio (height > width).
       - `face`: Executes OpenCV Haar Cascade facial detection to guarantee a human face is present.
       - `min_res`: Enforces minimum resolution (e.g. 600px).
  3. **Bank Account Letter**:
     - Detection: Keywords (*iban, swift, account number, bank, confidential, account number and iban confirmation*).
     - Validation Checks:
       - Subject confirmation regex: `account\s*number\s*and\s*iban\s*confirmation`.
       - IBAN regex: Standard international regex matching `[A-Z]{2}\d{2}[A-Z0-9]{11,30}`.
       - SWIFT / BIC regex: Detects `swift|bic`.
       - Account number regex: Matches `account\s*(no|number|#)`.
       - Date verification: Detects issue dates and runs `doc_age` verification (must be issued within the last 90 days).
       - Confidentiality statement check: Matches `confidential`.
       - Identity checks: `name_in_text` and `surname_in_text`.
       - Anti-Fraud Check (`no_sample_terms`): Flags documents containing placeholder words (*sample, specimen, template, John Doe*).
       - Manual verification items: Letterhead & logo, officer signature, official branch stamp.
  4. **Medical Report**:
     - Detection: Keywords (*igra, hiv, hcv, hbsag, medical report, laboratory*).
     - Validation Checks:
       - Test result presence: IGRA (Quantiferon/T-Spot), HIV, HCV (Hepatitis C), HBsAg (Hepatitis B).
       - Identity alignment: Name and surname match passport.
       - Document Recency: Enforces issue date within the last 180 days (`doc_age`).
- **Custom Requirement Builder**: Allows adding unlimited new document requirements with custom scoring rules, keywords, regexes, and checks directly via the GUI.

### 2.3 Optical Character Recognition & Image Processing
- **Tesseract OCR Integration**: Uses `pytesseract` with configurable language models (`ocr_lang`, e.g. `eng+ara`).
- **Selective Page OCR**: Reads native text layer first via `PyMuPDF` (`fitz`); OCRs only pages lacking digital text.
- **Automatic Orientation Correction (`feat_autorotate`)**:
  - Leverages Tesseract Orientation and Script Detection (OSD) to detect sideways (90°, 270°) or upside-down (180°) scans and automatically turns them upright before merging.
- **Automatic Deskewing (`feat_deskew`)**:
  - Uses OpenCV and NumPy Hough transform line detection to calculate scan tilt angles and straightens skewed pages.
- **MRZ Dedicated OCR (`ocr_mrz`)**:
  - Crops the bottom 25% of passport scans, enhances contrast, and runs specialized OCR configured for machine-readable zones.

### 2.4 Multi-Page PDF Segmentation & Page Assignment
- **Split-PDF Capabilities (`split_pdf`)**:
  - Automatically or manually breaks multi-page PDF documents into individual requirements (e.g. `Document.pdf#pages=1-2` assigned to Passport, `Document.pdf#pages=3-4` assigned to Medical).
- **Interactive Page Picker Dialog**:
  - Visual grid of all pages in a document.
  - Allows selecting specific page ranges to assign to any requirement.

### 2.5 Applicant Profile Management (`feat_profiles`)
- **Profile Fields**: Name, Surname, Job Title, Phone, Joining Date (Day, Month, Year pickers), Passport Number, Date of Birth (DOB), Passport Expiry.
- **Passport Auto-Fill (`feat_autofill`)**:
  - Parses passport text and MRZ lines (ICAO 9303) to automatically extract and populate applicant Name, Surname, Passport Number, DOB, and Expiry Date.
- **Saved Applicant Database**:
  - Stores applicant profiles in `docpacker_config.json`.
  - Dropdown selector to switch between saved applicants, create new applicants, or delete existing profiles.

### 2.6 PDF Compilation, Cover Page & Formatting
- **Automated Cover Page (`draw_cover`)**:
  - Generates an executive cover page displaying:
    - Package Title (e.g. "Application Package").
    - Profile Information Grid: Full Name, Job Title, Phone, Joining Date, Passport No, DOB, Expiry Date.
    - Comprehensive Verification Checklist: Displays every requirement, assigned file name, and status icons:
      - `✔` OK / Compliant.
      - `✖` Failed rule.
      - `⚠` Warning / Non-critical notice.
      - `☑` Manual check verified.
      - `☐` Manual check pending.
      - `○` Skipped / Optional item.
- **Multilingual & Arabic RTL Text Engine (`feat_arabic`)**:
  - Detects Arabic characters (`has_arabic()`).
  - Supports Arabic text shaping and bidirectional reordering (via `arabic_reshaper` and `python-bidi`) or system font fallbacks (`Noto Sans Arabic`, `Segoe UI`).
- **Interactive Bookmarks**: Creates a hierarchical PDF Table of Contents with bookmarks linking to the Cover Page and each individual document section.
- **Output PDF Compression (`feat_compress`)**:
  - Compresses the final document to meet strict upload file size caps (`target_size_mb`).
  - Uses PyMuPDF deflate compression, image resolution re-sampling, and quality adjustments.
- **Dynamic File Naming**: Configurable output template (e.g. `{name}_{date}`).

### 2.7 Interactive GUI & Workflow Tools
- **Split-View Main Window**:
  - Left: Requirements List with status badges, applicant details form, and action buttons.
  - Right: Document thumbnail visual flow + verification details tab.
- **Visual Thumbnail Preview (`feat_preview`)**:
  - Renders thumbnails of all assigned pages.
  - Click-to-enlarge high-resolution preview dialog.
- **Undo System (`feat_undo`)**: Multi-level Undo (`Ctrl+Z`) capturing file assignments, unassignments, and profile edits.
- **Verification Report Export**: Generates standalone text or HTML compliance verification reports (`write_report`).
- **Audit History Log (`feat_log`)**: Appends all packaging events to `docpacker_history.log`.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```python
# DOCPACKER PIPELINE PSEUDO-SCRIPT

def run_docpacker():
    config = load_config("docpacker_config.json")
    app_state = AppState(config)
    
    # 1. Ingest Files (via UI Drag&Drop, File Dialog, or Watch Folder)
    files = app_state.get_input_files()
    
    # 2. Extract Document Data
    for file in files:
        doc_info = extract_metadata_and_text(file)
        if doc_info.needs_ocr and config.feat_autorotate:
            doc_info.rotation = detect_rotation(doc_info.image) # Tesseract OSD
            doc_info.image = apply_rotation(doc_info.image, doc_info.rotation)
        if doc_info.needs_ocr and config.feat_deskew:
            doc_info.skew_angle = detect_skew(doc_info.image)   # Hough lines
            doc_info.image = apply_deskew(doc_info.image, doc_info.skew_angle)
            
        doc_info.text = ocr_or_read(file)
        app_state.documents.append(doc_info)
        
    # 3. Match Documents to Requirements
    for req in config.reqs:
        best_doc, score = find_best_matching_document(req, app_state.documents)
        if score >= req.min_score:
            app_state.assignments[req.id] = best_doc
            
    # 4. Optional Passport Autofill
    if config.feat_autofill and "passport" in app_state.assignments:
        passport_text = app_state.assignments["passport"].text
        mrz_data = parse_mrz(passport_text)
        if mrz_data:
            app_state.profile.update_from_mrz(mrz_data)
            
    # 5. Run Compliance Checks
    for req in config.reqs:
        doc = app_state.assignments.get(req.id)
        for check in req.checks:
            result = run_check(check, doc, app_state.profile)
            # Checks:
            # - mrz_valid: calculate ICAO 9303 checksums
            # - passport_expiry: expiry_date >= joining_date + 6 months
            # - doc_age: issue_date >= today - max_days
            # - face: cv2.CascadeClassifier face count >= 1
            # - portrait: img.height > img.width
            # - no_exif_camera: "Make"/"Model" in EXIF == False
            # - name_matches: fuzzy string distance <= threshold
            app_state.check_results[req.id].append(result)
            
    # 6. User Review & Manual Check Confirmation
    ReviewUI.display_checklist_and_preview(app_state)
    
    # 7. PDF Compilation
    if user_clicks_combine():
        merged_pdf = fitz.open()
        
        # A. Draw Cover Page
        if config.cover_page:
            cover_page = merged_pdf.new_page(width=A4_W, height=A4_H)
            draw_cover_header(cover_page, app_state.profile)
            draw_cover_checklist(cover_page, app_state.check_results)
            
        # B. Append & Rotate Assigned Documents
        for req in config.reqs:
            doc = app_state.assignments.get(req.id)
            if doc:
                start_page = len(merged_pdf)
                append_document(merged_pdf, doc, apply_fixes=True)
                if config.bookmarks:
                    merged_pdf.set_toc_entry(title=req.name, page=start_page)
                    
        # C. Compress if needed
        if config.feat_compress:
            merged_pdf = compress_pdf(merged_pdf, max_mb=config.target_size_mb)
            
        # D. Save Output
        output_path = format_output_name(config.output_name, app_state.profile)
        merged_pdf.save(output_path)
        write_audit_log(output_path, app_state)
```

---

## 3. Configuration & Feature Flags Reference Table

| Flag / Setting Key | Default | Description |
|---|---|---|
| `feat_preview` | `True` | High-resolution visual thumbnail grid and page selector |
| `feat_autorotate` | `True` | Automatically rotates sideways or upside-down scans via OSD |
| `feat_deskew` | `True` | Straightens slightly tilted documents using Hough transforms |
| `feat_datecheck` | `True` | Validates passport validity vs joining date and document age |
| `feat_mrz` | `True` | Verifies ICAO 9303 machine-readable zone checksum digits |
| `feat_autofill` | `True` | Automatically populates profile fields from passport MRZ |
| `feat_compress` | `False` | Compresses final PDF to meet `target_size_mb` constraint |
| `feat_profiles` | `True` | Enables persistent database of saved applicant profiles |
| `feat_batch` | `True` | Batch processing mode across subfolders |
| `feat_arabic` | `True` | RTL Arabic font rendering on cover page |
| `feat_dupes` | `True` | Shingle-based duplicate and overlap detection |
| `feat_undo` | `True` | Full multi-step undo history (`Ctrl+Z`) |
| `feat_log` | `True` | Append-only audit trail in `docpacker_history.log` |
| `feat_validate` | `True` | Self-diagnostic validator for external tool dependencies |
| `watch_folder` | `""` | Directory to monitor for arriving applicant files |
| `output_folder` | `""` | Output directory for combined PDF packages |
| `cover_page` | `True` | Inserts summary cover page at the beginning of the PDF |
| `cover_checklist` | `True` | Embeds compliance checklist table on the cover page |
| `bookmarks` | `True` | Creates clickable PDF Table of Contents bookmarks |
| `target_size_mb` | `5` | Maximum PDF file size limit when compression is enabled |
| `split_pdf` | `True` | Enables multi-page PDF page range extraction |

