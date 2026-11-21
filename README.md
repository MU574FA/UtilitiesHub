# UtilitiesHub 🛠️

A modern, offline-first desktop productivity suite built with Python and Tkinter. UtilsHub integrates document sorting, OCR extraction, PDF packaging, Excel synchronization, fuzzy matching, and visual workflow automation into a single cohesive interface.

---

## ✨ Features

- **🛫 Visa Tracker**: Never miss a visa window again. Tracks every person's visa (default 90 days), shows days left with colour codes, tells you **who to contact today**, keeps a **proof log** of every follow-up, stores *just the visa page* cut out of a big file, tracks tickets and flights, and warns about problems (flight after the deadline, no ticket, passport too short). See [Visa Tracker](#-visa-tracker).
- **📑 CV Sorter**: Automatically inspects, renames, and sorts incoming resumes (PDF, DOCX, PPTX, images) based on candidate names, contact details, and job titles with fuzzy matching.
- **📦 DocPacker**: Assembles comprehensive document dossiers and visa/application packages with automatic cover pages, page rotation, deskewing, and validation.
- **📄 PDF Desk (Multi-file Merger)**: Combine, split, reorder, rotate, and optimize documents with instantaneous thumbnail previews and drag-and-drop.
- **🔎 Extract** (tabs: *Screenshots · Find text · Passports*): Extract text, dates, phone numbers and Arabic text from screenshots, scans and photos into a table; search any file for words or patterns (regex or similarity); read passports (MRZ with check digits, expiry, validity).
- **🔄 Match & Sync** (tabs: *Names · Sheet sync*): Compare two name lists or find typed names in a file (typos, word order, Arabic spelling), and update an old Excel sheet from a new one by key, reviewing every change before it is applied.
- **🧼 Clean-up** (tabs: *Duplicates · Scans*): *Duplicates* finds identical files, documents with the same text and look-alike scans (even turned, re-sized or re-saved), each with a likelihood score; work in **Manual**, **Assisted** or **Auto** mode. Duplicates are *moved* to a review folder, never deleted. *Scans* detects turned, tilted, shadowed and blank pages and fixes them (turn upright, straighten, trim, even out lighting, black & white, drop blank pages), then lets you fine-tune any page by hand. Originals are never touched, and a whole batch is undone in one click.
- **🤖 Automation** (tabs: *Workflows · Autopilot · History & Undo*): Snap together Lego-like bricks (*Filter → OCR → Rename → Merge → Move*) with dry-run previews; let **folder triggers** run a saved workflow automatically whenever a file arrives in a folder (with a loop guard and a *Test (preview)* button); and search, export or undo anything that was ever moved, copied or renamed - one click undoes a whole run.
- **⌨ Autocomplete**: Every text box has its own keywords and shortcuts, and you decide where it is on or off (Settings > Autocomplete, or right-click any box). See [Autocomplete](#-autocomplete).
- **🏠 Customisable Home**: Drag tools into your own order, hide the ones you do not use and bring them back with one click (Settings > Home & tools, or right-click a tool in the sidebar / on Home).
- **⏪ Journal & Undo**: Full operational journaling allowing one-click rollback of all file renames, copies, and moves (Automation > History & Undo).
- **🗂️ Tabs with a shared files bar**: Related tools live together. Drop files once, then send the same files to any tab of that tool with one click. The last tab is remembered; **Alt+←/→** switches tabs; the command palette (Ctrl+K) still finds every tab's commands.
- **💬 WhatsApp import (DocPacker)**: *💬 WhatsApp ZIP…* (or drop the exported ZIP) lists the files with their sender, date and caption, previews each one, lets you mark which file is which document (or that one file holds several), shows the chat like WhatsApp and fills the name, phone, joining date or any field from a message you pick. Files are unzipped one at a time, only when opened, and deleted after a few days.
- **✨ Optional offline AI (Ollama)**: Read scans with a local vision model, fill fields in plain words, recognise and split documents, answer checklist questions, compare names across spellings and fill the DocPacker cover page - all through your own Ollama, all switchable, with a **Rules / AI / Rules→AI / AI→Rules** mode for every feature. See [AI (optional)](#-ai-optional-ollama-any-openai-compatible-api-or-anthropic).

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

## ⌨ Autocomplete

Suggestions and text shortcuts while you type, in the CV Sorter / Screenshot field forms, the Visa Tracker (person form, contact-log note) and the DocPacker cover fields.

**You decide where.** *Settings > Autocomplete* lists every box. Click the ✔ in front of a box to switch it on or off, or use *Enable / Disable all shown*. The master switch (and *Ctrl+K > Turn autocomplete on / off*) silences everything without losing your choices. You can also **right-click any box** → *turn off for this box*, *add the selected text as a keyword*, *add a shortcut…*, *Keywords & shortcuts for this box…*.

**Each box has its own**
- **Keywords**: words and phrases offered while typing (one per line, import from a .txt/.csv). Some boxes also use the word lists from *Settings > Matching* (job titles, known companies, visa types).
- **Shortcuts**: `nr = No response`. Type the shortcut, then **Space** or **Tab**; Backspace straight after puts it back. A shortcut starting with a symbol works mid-word (`john@g` → `john@gmail.com`). In the text you can use `{today}`, `{time}`, `{cursor}` and `\n` (new line, in multi-line boxes).
- **Remember what I type**: finished values (names, groups, companies) are offered next time. Kept only in `data/autocomplete_learned.json`, never in exported settings. Boxes holding passport, phone, visa or flight numbers are never remembered.

**Keys:** ↑/↓ choose, **Tab** accepts, **Enter** accepts only a suggestion you chose with the arrows (so Enter still submits a form), **Esc** closes, **Ctrl+Space** opens the list. Matching is accent-, case- and Arabic-spelling-insensitive; *Smart* matching also understands initials (`hro` → Human Resources Officer) and one typo.

**Developers:** `core/autocomplete.py` (engine, no Tk, `tests/test_autocomplete.py`), `ui/autocomplete.py` (`attach(widget, "field.id", extra=...)`), `ui/autocomplete_ui.py` (settings page). To add a box: register a `FieldDef` (or a source for settings-dependent boxes) and call `attach`.

---

## 🛫 Visa Tracker

For HR teams that recruit from abroad and pay for visas that must be used within a fixed window (default **90 days**).

**Daily use**
1. Open *Visa Tracker*. The sidebar button shows how many people need contact **today**; the **To do today** chip lists them.
2. Contact the person (📞 *Spoke*, 📵 *No answer*, 💬 *Sent* are one click; *Log…* (Ctrl+L) writes a full entry with a note and a next reminder date).
3. When they land: **✔ Arrived**. Reminders stop.

**Adding people**: *＋ Add person* (Ctrl+N, with *Save & add another*), *Import…* from Excel/CSV (columns are matched automatically; one Ctrl+Z undoes a whole import) or *Fill from passport scan* inside the form.

**The visa page**: in a person's *Documents* tab choose *Visa page from a big file…* (or drop the file on the tool). Every page is shown as a thumbnail, the likely visa page is marked ★ (found by visa wording, the passport number and the name) and the issue / valid-until dates are read from it. Only that single page is stored. Tickets work the same way (a stored ticket marks the ticket as booked).

**How often is a person contacted?** Settings > Visa Tracker > *How often to contact a person*. Default `60:14, 30:7, 14:3, 0:1` = every 14 days with 60+ days left, every 7 from 30, every 3 from 14 and **daily** when fewer than 14 days are left. After *No answer* the person is due again the next day.

**Proof**: every contact is saved with date, time, channel, result, note and your name (*Settings > Your name*). *Export > Follow-up proof log* gives an Excel file per person or for everybody.

**Messages**: ready WhatsApp / e-mail texts with {placeholders} (editable, one is picked automatically by urgency). After opening WhatsApp or e-mail the tracker asks whether to log it as sent.

**Safety net**: every change is saved at once; a dated backup is kept daily (14 by default); Ctrl+Z / Ctrl+Y undo and redo; files are never deleted; if the data file is damaged the newest backup is loaded. *Export > Calendar file (.ics)* puts every deadline (with alarms 14/7/3/1 days before), follow-up and flight into Outlook / Google Calendar.

**Data location**: `<data folder>/visa_tracker/` (`people.json`, `files/`, `backups/`). It contains passports and phone numbers: keep the folder on a protected disk. You can point *Settings > Data* to a shared or synced folder (copy the folder there first). Several PCs can read and write it; the program reloads changes made elsewhere (the last save wins if two people change the same second).

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

Everything UtilsHub saves stays inside the project folder, in `./data`:
- `settings.json` – all settings
- `journal.jsonl`, `cv_history.csv`, `workflows.json`, `visa_tracker/` – history and tool data
- `utilshub.log`, `cache/`, `tmp/` – logs, render caches and scratch files
- `workflow_output/` – default output of workflows (unless you pick another folder)

Move the project folder and your data moves with it. On first start, data from an older install (`%APPDATA%\UtilsHub`, `~/Library/Application Support/UtilsHub` or `~/.config/utilshub`) is copied into `./data` once.
Set the `UTILSHUB_HOME` environment variable only if you want the data somewhere else.

---

## ✨ AI (optional: Ollama, any OpenAI-compatible API, or Anthropic)

AI is **off by default**; with it off the program behaves exactly as before. You choose where the model runs:

| Provider | You enter | Examples |
|---|---|---|
| **Ollama** (local) | address + model name | `http://localhost:11434`, `llama3.2`, `qwen2.5vl` |
| **OpenAI-compatible API** | address + API key + model name | OpenAI, Groq, OpenRouter, Together, Mistral, DeepSeek, Google Gemini (`https://generativelanguage.googleapis.com/v1beta/openai`), LM Studio / vLLM |
| **Anthropic** | API key + model name | Claude models |

1. Local: install [Ollama](https://ollama.com), then `ollama pull llama3.2` and `ollama pull qwen2.5vl`. Cloud: have an API key ready (or set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`).
2. Open **Settings > AI**, tick **Enable AI**, pick the **Provider**, fill address / key / models, press **Test connection**. A server outside your computer / local network is used only when **Allow cloud AI** is ticked - the text and pictures of your documents are then sent to that provider.
   - **Per-task models:** under *optional override* every capability can use its own provider / address / key / model: **Reasoning** (yes/no questions, name checks), **Extraction** (fields, short answers), **Classification** (document types, categories) and **Vision** (AI OCR, scans). Empty boxes inherit the default connection - e.g. a small local model for classification and a Claude or GPT model for vision.
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

**Safety:** AI values are only accepted if they really appear in the document (switchable), have the right shape (e-mail, phone ...) and are always marked ✨ for you to check: CV Sorter sends such files to review, DocPacker lists them, never overwrites what you typed and asks before building a package from AI-recognised documents. Optional *cross-check* runs both methods and flags disagreements. If the AI server is not reachable (or the key is rejected) the Rules are used (or, in *AI only*, the field stays empty with a clear message).

---

## 🧪 Running Tests

To run the test suite:
```bash
pip install pytest
pytest
```
The AI tests (`tests/test_ai.py`, `tests/test_ai_ui.py`) are fully offline: a fake Ollama server (`tests/fake_ollama.py`) answers every request, so no model is needed; `tests/test_llm_providers.py` does the same for the OpenAI-compatible and Anthropic providers and the per-task models (`tests/fake_providers.py`).

---

## 🔒 Privacy & Local Processing

- UtilsHub runs **100% locally on your machine**.
- No documents, images, or extracted text are uploaded to any cloud services.
- The optional AI features are off until you enable them and only talk to the addresses you set (default Ollama on `http://localhost:11434`, i.e. this computer). Anything outside this computer / local network (cloud APIs) is refused unless **Allow cloud AI** is ticked; then the text and pictures of your documents are sent to that provider. API keys are stored in the settings file (not in *Export settings*); the environment variables `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` work too.
- The repository includes a preconfigured `.gitignore` to prevent any personal documents, resumes, spreadsheets, or runtime journals from ever being tracked in version control.

---

## 📜 License

This project is licensed under the [MIT License](LICENSE).
