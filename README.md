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
- **✨ Optional offline AI (Ollama)**: Read scans with a local vision model, fill fields in plain words, recognise and split documents, answer checklist questions, compare names across spellings and fill the DocPacker cover page - all through your own Ollama, all switchable, with a **Rules / AI / Rules→AI / AI→Rules** mode for every feature. See [AI (optional)](#-ai-optional-offline-with-ollama).

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

## ✨ AI (optional, offline with Ollama)

AI is **off by default**; with it off the program behaves exactly as before. Everything runs through [Ollama](https://ollama.com) on your own computer (no cloud, no API keys).

1. Install Ollama, then pull a text model and a vision model, e.g. `ollama pull llama3.2` and `ollama pull qwen2.5vl`.
2. Open **Settings > AI (Ollama)**, tick **Enable AI**, press **Test connection**.
3. Choose a mode for each feature. The classic way is called **Rules** (regex, keywords, Tesseract):

| Mode | Meaning |
|---|---|
| Rules only | Exactly the classic behaviour |
| AI only | The model decides; if it fails the result stays empty |
| Rules first, AI if they find nothing | AI is only asked when the Rules fail |
| AI first, Rules if it finds nothing | Rules are the safety net |

| Feature (own mode each) | What the AI does |
|---|---|
| Reading scans (OCR) | A vision model reads pictures / scanned pages instead of (or after) Tesseract. Used by every tool and workflow. |
| Filling fields (CV Sorter, Screenshot) | Per field: *Detector → AI for this field* (default / rules / AI / either order) and *Tell the AI what to look for*. A new detector kind **AI-described** needs no rules at all. |
| Recognising documents (DocPacker) | Picks the document type of a file or page and suggests where a scan splits into several documents. Each type can opt out and carries an *AI description*. |
| AI questions in checklists (DocPacker) | New check kind **AI question (yes / no)**, optionally looking at the page picture (signature, stamp). Rules only = you tick it yourself; AI first = you are asked only when the AI is unsure. |
| Comparing names (DocPacker) | "Name appears in the document" / "surname matches" understand spelling variants (Mohammed / Muhammad), word order and OCR slips. |
| Cover-page values (DocPacker) | **✨ Fill from documents** (or automatically after reading) fills *empty* name, surname and custom boxes. |
| Workflows | Bricks **AI read the text**, **AI grab a value**, **AI sort into categories**, **AI rename**; each has its own method. Grabbed values work as rename tokens, e.g. `{category}`. |

**Safety:** AI values are only accepted if they really appear in the document (switchable), have the right shape (e-mail, phone ...) and are always marked ✨ for you to check: CV Sorter sends such files to review, DocPacker lists them, never overwrites what you typed and asks before building a package from AI-recognised documents. Optional *cross-check* runs both methods and flags disagreements. If Ollama is not running the Rules are used (or, in *AI only*, the field stays empty with a clear message).

---

## 🧪 Running Tests

To run the test suite:
```bash
pip install pytest
pytest
```
The AI tests (`tests/test_ai.py`, `tests/test_ai_ui.py`) are fully offline: a fake Ollama server (`tests/fake_ollama.py`) answers every request, so no model is needed.

---

## 🔒 Privacy & Local Processing

- UtilsHub runs **100% locally on your machine**.
- No documents, images, or extracted text are uploaded to any cloud services.
- The optional AI features only talk to the Ollama address you set (default `http://localhost:11434`, i.e. this computer) and are off until you enable them. If you point it at another machine, the text of your documents is sent there.
- The repository includes a preconfigured `.gitignore` to prevent any personal documents, resumes, spreadsheets, or runtime journals from ever being tracked in version control.

---

## 📜 License

This project is licensed under the [MIT License](LICENSE).
