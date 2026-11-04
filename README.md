# UtilitiesHub 🛠️

A modern, offline-first desktop productivity suite built with Python and Tkinter. UtilsHub integrates document sorting, OCR extraction, PDF packaging, Excel synchronization, fuzzy matching, and visual workflow automation into a single cohesive interface.

---

## ✨ Features

- **📑 CV Sorter**: Automatically inspects, renames, and sorts incoming resumes (PDF, DOCX, PPTX, images) based on candidate names, contact details, and job titles with fuzzy matching.
- **📦 DocPacker**: Assembles comprehensive document dossiers and visa/application packages with automatic cover pages, page rotation, deskewing, and validation.
- **📄 PDF Desk (Multi-file Merger)**: Combine, split, reorder, rotate, and optimize documents with instantaneous thumbnail previews and drag-and-drop.
- **🧱 Visual Workflows**: Snap together Lego-like functional bricks (*Filter → OCR → Rename → Merge → Move*) with safe dry-run previews and reversible execution.
- **📊 Sheet Sync & Name Matcher**: Reconcile, fuzzy match, and sync tables between Excel spreadsheets and unstructured document collections.
- **📸 Screenshot & Data Extractor**: Extract text, MRZ passport codes, dates, phone numbers, and Arabic text via integrated OCR.
- **🧬 Duplicate Finder**: Finds identical files, documents with the same text and look-alike scans (even turned, re-sized or re-saved). Each match gets a likelihood score; work in **Manual**, **Assisted** (confident matches pre-ticked) or **Auto** mode. Duplicates are *moved* to a review folder, never deleted, and a whole batch is undone in one click.
- **🧼 Scan Cleaner**: Detects turned, tilted, shadowed and blank pages in images and PDFs and fixes them automatically (turn upright, straighten, trim borders, even out lighting, black & white, drop blank pages) - then lets you fine-tune any page by hand (tilt slider, drag-to-crop, before/after). Originals are never touched.
- **🏠 Customisable Home**: Drag tools into your own order, hide the ones you do not use and bring them back with one click (Settings > Home & tools, or right-click a tool in the sidebar / on Home).
- **⚡ Autopilot**: Background folder watcher automation that processes incoming files according to defined rules.
- **⏪ Journal & Undo**: Full operational journaling allowing one-click rollback of all file renames, copies, and moves.

---

## 🚀 Getting Started

### Prerequisites

- Python 3.10 or higher
- Optional external tools:
  - **Tesseract OCR** (for text recognition from scans and images)
  - **LibreOffice** (for automated DOCX/PPTX to PDF conversions and previews)

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/UtilsHub.git
   cd UtilsHub
   ```

2. **Create and activate a virtual environment (recommended):**
   ```bash
   # Windows (PowerShell)
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1

   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Launch the application:**
   ```bash
   python run.py
   ```
   *(On Windows, you can also double-click `Start-UtilsHub.bat`)*

---

## ⌨️ Shortcuts & Controls

| Shortcut | Action |
| :--- | :--- |
| `Ctrl + K` | Open quick command palette |
| `Ctrl + ,` | Open Settings |
| `Alt + 1` .. `Alt + 9` | Switch between tools |
| `F11` | Toggle Full Screen |
| `Drag & Drop` | Drop files directly onto the window or active tool |

---

## ⚙️ Configuration & Data Storage

UtilsHub stores settings, journals, and logs in your standard user application directory:
- **Windows**: `%APPDATA%\UtilsHub`
- **macOS**: `~/Library/Application Support/UtilsHub`
- **Linux**: `~/.config/utilshub`

> **Portable Mode:** Create an empty file named `portable.flag` next to `run.py` (or set the `UTILSHUB_HOME` environment variable) to store all settings and logs locally inside a `./data` directory.

---

## 🧪 Running Tests

To run the test suite:
```bash
pip install pytest
pytest
```

---

## 🔒 Privacy & Local Processing

- UtilsHub runs **100% locally on your machine**.
- No documents, images, or extracted text are uploaded to any cloud services.
- The repository includes a preconfigured `.gitignore` to prevent any personal documents, resumes, spreadsheets, or runtime journals from ever being tracked in version control.

---

## 📜 License

This project is licensed under the [MIT License](LICENSE).
