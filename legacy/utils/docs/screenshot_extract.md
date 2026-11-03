# Screenshot Extract — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**Screenshot Extract** is an automated desktop assistant that streamlines the transfer of data from visual screen captures (e.g. taken via Windows Snipping Tool / `Win+Shift+S`) into structured spreadsheet rows. By continuously monitoring the system clipboard for new images, it applies image enhancement and Tesseract OCR, parses specific data fields (names, telephone numbers, emails, job titles, IBANs, qualifications, salaries, etc.), displays a side-by-side verification popup, and formats the output as clean tab-separated text ready for one-click pasting into Microsoft Excel, Google Sheets, or web CRM tables.

---

## 2. Exhaustive Feature Inventory

### 2.1 Clipboard Monitoring & Image Ingestion
- **Automated Polling Loop (`tick`)**: Inspects the clipboard at a configurable polling frequency (`POLL_MS = 500` ms) using `PIL.ImageGrab.grabclipboard()`.
- **Hybrid Source Handling**: Accepts raw pixel bitmap data directly from the clipboard as well as copied image files from the Windows Explorer shell.
- **MD5 Deduplication Safeguard**: Computes an MD5 checksum hash of the image bytes (`hashlib.md5(img.tobytes()).hexdigest()`) to ensure screenshots are processed exactly once and prevent duplicate OCR runs.

### 2.2 OCR Engine & Image Enhancement (`ocr`)
- **Tesseract Integration**: Directly interfaces with Tesseract OCR with configurable binary executable path (`TESSERACT_PATH`) and multilingual model selection (`OCR_LANG`, e.g. `eng` or `eng+ara`).
- **Contrast & Resolution Preprocessing (`ENABLE_PREPROCESS`)**:
  - Automatically checks screenshot dimensions: if the width is under 1600 pixels, it enlarges the image by 2x using Lanczos resampling (`Image.LANCZOS`).
  - Applies automated histogram contrast optimization (`ImageOps.autocontrast`) to clarify faded text or low-contrast backgrounds.
  - Converts images to single-channel grayscale (`L`).
- **Arabic-Indic Digit Translation (`ENABLE_ARABIC_DIGITS`)**: Optional translation table mapping Eastern Arabic numerals (`٠١٢٣٤٥٦٧٨٩`) to standard digits (`0123456789`).
- **OCR Repair Heuristics (`ENABLE_EMAIL_REPAIR`)**: Automatically strips erroneous whitespace characters inserted around `@` symbols by OCR engines.

### 2.3 Modular Extractor Architecture (`EXTRACTORS`)
The engine features eight specialized extractor classes capable of parsing diverse data types:
1. **`phone` Extractor**:
   - Matches numbers using the international phone regex: `(\+?\d[\d\s\-().]{7,}\d)`.
   - **Normalization Engine (`normalize_phone`)**: Strips formatting symbols, converts international prefix `00` to bare digits, prepends configurable `DEFAULT_COUNTRY_CODE` to local numbers starting with `0`, and validates that the digit count falls between 9 and 15 digits.
2. **`email` Extractor**:
   - Matches valid email addresses using `[\w.+-]+@[\w-]+(?:\.[\w-]+)+`.
   - Normalizes to lowercase and trims trailing periods.
3. **`name` Extractor**:
   - Inspects the top 15 lines of OCR text.
   - Filters out non-name lines using an exclusion stopword dictionary (`NOT_A_NAME`: *cv, resume, curriculum, vitae, whatsapp, online, typing, last seen, email, phone, mobile, address, profile, objective, experience, education, skills, etc.*).
   - Validates that the line has between 2 and 4 words, contains no numbers or `@` symbols, does not exceed 40 characters, and maintains > 90% alphabetic letter density.
   - Capitalizes ASCII names to Title Case.
4. **`keywords` Extractor**:
   - Matches target terminology from predefined keyword lists or dictionary mappings (`{canonical: [variants]}`).
   - Sorts candidates by position and length (earliest match wins; longer phrases win ties).
   - Substring suppression: Prevents picking a shorter word contained inside an already matched term (e.g. ignores "Engineer" if "Mechanical Engineer" matched).
   - **Multi-Match Support**: Option `all_matches=True` joins all matched keywords with commas (e.g. for Skills).
5. **`regex` Extractor**:
   - Executes arbitrary regular expressions.
   - Extracts capture group 1 when defined, or the full match.
   - Supports case-sensitive or case-insensitive matching.
6. **`after_label` Extractor**:
   - Extracts text following specified label prefixes (e.g. `Location: Baghdad`, `Expected Salary: $2,500`).
   - Configurable list of target labels with maximum length caps (`max_len`).
7. **`today` Extractor**:
   - Dynamically generates the current date formatted according to `DATE_FORMAT` (default: `%d-%b-%Y`, e.g. `31-Oct-2026`).
8. **`manual` Extractor**:
   - Produces an empty text box in the popup for notes, comments, or manual data entry.

### 2.4 Pre-Configured Field Catalog (`FIELDS`)
The application comes pre-equipped with an extensive library of ready-to-use fields:
- **Core Fields**:
  - `phone` (Phone number).
  - `email` (Email address).
  - `name` (Candidate full name).
  - `title` (Job title keywords).
  - `iban` (International Bank Account Number regex).
  - `date` (Today's date).
  - `notes` (Manual notes field).
- **Extensible Optional Lookups**:
  - `nationality` (Dictionary mapping: Iraqi, Indian, Egyptian, Pakistani, Bangladeshi, Nepali).
  - `gender` (Male, Female).
  - `age` (Regex matching `\bage\s*[:\-]?\s*(\d{2})\b`).
  - `experience` (Regex matching `(\d{1,2})\+?\s*(?:years?|yrs?)\b`).
  - `education` (Canonical dictionary: PhD, Master, Bachelor, Diploma).
  - `location` (Labels: Location, Current Location, City, Address).
  - `id_number` (Labels: National ID, ID No, Passport No).
  - `salary` (Labels: Expected Salary, Salary Expectation, Salary).
  - `linkedin` (Regex matching LinkedIn profile URLs).
  - `skills` (Multi-match keywords: AutoCAD, Excel, PLC, Welding, Forklift, Safety, SAP).

### 2.5 Dual-Window Interactive GUI

#### Control Panel (`Panel`)
- Pinned, topmost floating window (`PANEL_ALWAYS_ON_TOP`).
- Grid of checkboxes for every configured field, arranged in clean columns.
- Toggles for "Show compare popup" and "Copy result automatically".
- **"Re-scan last screenshot" Button**: Allows tweaking checkboxes and immediately re-evaluating the current screenshot without retaking it.
- **Settings Persistence**: Saves panel states to `extractor_settings.json` (`REMEMBER_PANEL_CHOICES`).

#### Verification & Correction Popup (`Popup`)
- Opens at the top-right of the screen alongside the control panel.
- Displays a visual thumbnail preview of the screenshot (constrained to `PREVIEW_SIZE`, default 640x360).
- Vertically aligned form with Consolas-font input boxes for every active field.
- **Alert & Notice Panel**:
  - Red warning text indicating missing expected fields (*"No phone found, type it in"*).
  - Secondary matches list (*"Other phone matches: +964..."*).
- **Single-Keystroke Dismissal**:
  - Pressing `Enter` or `Escape` immediately copies the row to the clipboard and closes the popup.
  - Clicking the window close button (`X`) also triggers copy-and-close.

### 2.6 Spreadsheet Formatting & Clipboard Integration
- **Customizable Column Order (`COLUMN_ORDER`)**: Output follows the user-defined column sequence; switched-off fields are skipped.
- **Tab-Separated Output (`row_text`)**: Concatenates cell values with tab characters (`\t`) and collapses internal newlines/spaces, ensuring flawless pasting across spreadsheet columns.
- **Auto-Copy Option (`ENABLE_AUTO_COPY`)**: Automatically places data on the clipboard immediately after extraction.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```python
# SCREENSHOT EXTRACT PSEUDO-SCRIPT

def run_screenshot_extractor():
    settings = load_settings("extractor_settings.json")
    last_image_hash = None
    
    while True:
        # 1. Poll Clipboard for Screenshot
        img = clipboard_grab_image()
        if img is None:
            sleep(POLL_MS)
            continue
            
        current_hash = md5(img.tobytes())
        if current_hash == last_image_hash:
            sleep(POLL_MS)
            continue
            
        last_image_hash = current_hash
        
        # 2. Image Preprocessing & OCR
        proc_img = img.convert("L")
        if proc_img.width < 1600:
            proc_img = proc_img.resize((proc_img.width * 2, proc_img.height * 2), LANCZOS)
        proc_img = autocontrast(proc_img)
        
        text = pytesseract.image_to_string(proc_img, lang=settings.ocr_lang)
        if settings.arabic_digits:
            text = translate_arabic_digits(text)
            
        # 3. Extract Active Fields
        values, alternates = {}, {}
        for field in settings.active_fields():
            candidates = EXTRACTORS[field.kind](text, field)
            if field.all_matches:
                values[field.key] = ", ".join(candidates)
                alternates[field.key] = []
            else:
                values[field.key] = candidates[0] if candidates else ""
                alternates[field.key] = candidates[1:]
                
        # 4. Auto-Copy to Clipboard
        tab_separated_row = "\t".join(values[f.key] for f in settings.active_fields_in_order())
        if settings.auto_copy and any(values.values()):
            pyperclip.copy(tab_separated_row)
            
        # 5. Show Interactive Review Popup
        if settings.show_popup:
            edited_values = show_popup_dialog(
                preview_image=img,
                fields=settings.active_fields_in_order(),
                values=values,
                alternates=alternates
            )
            # When user presses Enter or Esc:
            final_row = "\t".join(edited_values[f.key] for f in settings.active_fields_in_order())
            pyperclip.copy(final_row)
```

---

## 4. Configuration & Settings Reference Table

| Setting Key | Type | Default Value | Description |
|---|---|---|---|
| `SHOW_POPUP` | boolean | `True` | Display editable verification popup |
| `ENABLE_AUTO_COPY` | boolean | `True` | Copy tab-separated row immediately |
| `ENABLE_OTHER_MATCHES`| boolean| `True` | Show alternate candidate matches in popup |
| `SHOW_PANEL` | boolean | `True` | Floating main control window |
| `PANEL_ALWAYS_ON_TOP` | boolean | `True` | Keeps panel above other windows |
| `REMEMBER_PANEL_CHOICES`| boolean| `True` | Saves checkbox selections to JSON |
| `ENABLE_PREPROCESS` | boolean | `True` | 2x upscaling and autocontrast on images |
| `ENABLE_ARABIC_DIGITS`| boolean| `False` | Translates Eastern Arabic digits to 0-9 |
| `ENABLE_PHONE_NORMALIZE`| boolean| `True` | Formats phone numbers to standard digits |
| `ENABLE_EMAIL_REPAIR` | boolean | `True` | Fixes OCR whitespace around `@` |
| `TESSERACT_PATH` | string | `C:\Program Files\Tesseract-OCR\tesseract.exe` | Path to Tesseract binary |
| `OCR_LANG` | string | `eng` | Tesseract OCR language parameter |
| `DEFAULT_COUNTRY_CODE`| string| `""` | Country code prefix for zero-leading numbers |
| `DATE_FORMAT` | string | `%d-%b-%Y` | Date format string for `today` fields |
| `POLL_MS` | int | `500` | Clipboard polling interval in milliseconds |
| `PREVIEW_SIZE` | tuple | `(640, 360)` | Max thumbnail preview dimensions in popup |

