# CV Sorter

Drop a CV into a folder → the app reads it (OCR if needed), extracts name, phone, email,
job title, company and any custom fields you define, shows a **verification pop-up**, then
**renames** the file after the candidate and **moves** it to your output folder.

## Install

```bash
pip install -r requirements.txt
python main.py
```

External programs (not pip-installable):

| Needed for | Program |
|---|---|
| Images, scanned PDFs, pictures inside DOCX/PPTX | **Tesseract OCR** – Windows: <https://github.com/UB-Mannheim/tesseract/wiki> · macOS: `brew install tesseract` · Linux: `sudo apt install tesseract-ocr` |
| Other OCR languages (e.g. Arabic) | Tesseract language packs, then set *OCR languages* to `eng+ara` |
| Old `.doc` / `.ppt` / `.rtf` | LibreOffice (optional) |
| GUI on Linux | `sudo apt install python3-tk` |

Click **Tools → Test OCR** to verify Tesseract is found (or set its path in Settings → OCR).

## Supported files
PDF (text + scanned), DOCX (incl. headers, tables, text boxes), PPTX (incl. pictures),
images (PNG/JPG/TIFF/BMP/WEBP), TXT/MD/CSV, and DOC/PPT/RTF/ODT/ODP via LibreOffice.

## How it works
1. **Watcher** notices a new file and waits until it has finished copying.
2. **Reader** extracts text (OCR is used only when a page has little/no text).
3. **Extractors** find the fields.
4. **Pop-up** shows the extracted data **next to the original file**. You correct anything; the file
   name, generated links and name check update live. `Enter` = save & move, `Esc` = skip.
5. File is renamed with your template and moved/copied (or sent to *Duplicates*); a row is appended to
   `cv_history.csv` with its status (`saved` / `duplicate` / `recorded`).

## Similarity matching (typos & OCR errors)
OCR turns "Python" into "Pyth0n" and "Google" into "Gogle". The app tolerates that:
- **Keyword fields** – tick *Similarity match* on a keyword field (on by default).
- **Known companies** – Settings → Extraction → *Known companies* (one per line). Companies in this
  list are found even when misspelled and are written with **your** spelling. A detected company such
  as `Gogle LLC` is replaced by `Google` (*Replace a detected company…* turns this off). Known
  companies are placed first in the Company field, in order of appearance.
- **Job-title keywords** – optional (*Also tolerate typos in job-title keywords*, off by default).
- **Threshold** – default 80 %. Higher = stricter. Words under 5 letters (SQL, AWS, Git) and the first
  letter must match exactly, which keeps false positives low. The custom-field tester uses the threshold too.

## Clipboard (paste your values in the order you like)
Settings → **Clipboard** holds named *formats*. Each format is a template, built with the point-and-click
builder (pick fields, reorder, choose a separator) or typed by hand:

| Template | Pastes as |
|---|---|
| `{name}\t{job_title}\t{phone}` | three Excel / Google Sheets cells |
| `{phone} \| {name}` | `+1 555 0100 \| Ann Lee` |
| `{name}\n{email}` | two lines |

`{key}` = first value, `{key_all}` = every value (`; `-joined), `\t` = tab, `\n` = new line, plus
`{filename}`, `{date}`, `{original}` and your custom fields. Empty fields keep their column, so spreadsheet
columns never shift. Three examples are included (Excel row, One line, Contact card).

Ways to copy:
- In the verification pop-up: pick a format → **Copy now**, or tick **Also copy when saving**.
- Settings → *Copy to the clipboard automatically after Save & Move* (uses the default format).
- **Tools → Copy last record as…** re-copies the last saved CV in any format.

## Names list: "have I seen this person before?"
Settings → **Names list**: point to a `.txt` (one name per line) or `.csv` (first column, or the column
headed *Name*). The file is created if missing and re-read whenever you edit it.
- In the pop-up, the name is **re-checked every time you edit it**, using similarity (word order and
  accents ignored, extra middle names tolerated):
  - `✓ New name` – not in the list;
  - `⚠ Close to an existing name: John Smith (95%)` – shows the **full listed name**; **Use it** copies that
    spelling into the field;
  - `✖ Already in your list` – likely a duplicate.
- When you **Save**, the name is **appended to the file** automatically (switch off with *Add accepted
  names…*). Names sent to *Duplicates* are never added.
- With *Auto-confirm* on, a CV whose name is in (or close to) the list is **not** auto-saved; the pop-up
  opens so you can decide.
- Threshold: default 85 % (catches `Jon` / `John`). Lower = more variants, more false alarms.

## Duplicates & "don't move"
Three outcomes for every CV in the pop-up:
- **Save & Move** (or *Save & Copy* if Settings → Action is *copy*);
- **Send to Duplicates** – moves the file to your duplicates folder (Settings → General; empty = a
  `Duplicates` folder inside the output folder). It is renamed with the template and numbered
  `(2)`, `(3)`… (or keep the original name via the *Rename duplicates* option);
- **Skip** – leaves the file untouched in the inbox.

Plus the checkbox **Don't move the file (only record the data / copy to clipboard)**: Save then logs the
data, adds the name, copies to the clipboard, but the file stays where it is.

## Links (OneDrive / SharePoint / archive URLs)
Settings → **Links**: give a link a name and a template. Put your folder URL first and the file name last:

```
https://contoso-my.sharepoint.com/personal/hr/Documents/Archive/{filename}
```
- Placeholders: `{filename}` (with extension), `{stem}`, `{ext}`, `{date}`, `{original}`, and any field
  (`{name}`, `{company}`, custom fields…). Values are **URL-encoded** automatically
  (`John Smith.pdf` → `John%20Smith.pdf`); add `_raw` (`{name_raw}`) for no encoding.
- The link follows the **real final file name**, including `(2)` numbering if the name already exists.
- Each link shows in the pop-up with its own **Copy** button, is stored in `cv_history.csv`, and its name
  (lower_snake_case) is a field for clipboard formats: e.g. `{name}\t{archive_link}`.

## Original-file preview
The pop-up's right side has an **Original file** tab (rendered pages, scrollable) and an **Extracted text**
tab. Images and PDFs render directly; Word / PowerPoint / RTF / ODF are rendered through LibreOffice
(otherwise the text tab is shown); plain text uses the text tab. Rendering runs in the background so the
pop-up opens instantly. Turn it off or limit pages in Settings → General.

## Settings (Tools → Settings)
- **General** – folders, file-name template (`{name} - {job_title}`, `{date}`, `{original}`, …),
  duplicate handling (number / overwrite / skip), move or copy, auto-confirm, file types.
- **OCR & Converters** – Tesseract path, languages, DPI, LibreOffice path.
- **Extraction** – turn built-in fields on/off; tune job-title keywords, company suffixes,
  "words that are not names", the similarity threshold and the known-companies list.
- **Names list**, **Links**, **Clipboard** – see the sections above.
- **Custom fields** – add your own **regex** or **keyword list** with a live tester.
  Example: regex `(\d{1,2})\+?\s*years` → "Years of experience"; keywords `Python, SQL, AWS` → "Skills".
  Each custom field also becomes a `{placeholder}` (lower_snake_case) for the file name.

Settings are stored in `config.json` next to `main.py`; logs in `cv_sorter.log`.

## Project layout
```
main.py                     entry point
cvsorter/
  config.py                 Settings dataclasses + JSON load/save
  watcher.py                watchdog folder watcher (+ "file is stable" check)
  readers.py                one reader per file type (@register decorator)
  ocr.py                    Tesseract wrapper + image pre-processing
  extractor.py              built-in extractors (@builtin) + custom regex/keyword fields
  fuzzy.py                  similarity matching (RapidFuzz or difflib)
  clipboard.py              clipboard-format rendering
  registry.py               names list: similarity check + auto-append
  links.py                  URL templates built from the final file name
  preview.py                original-file page rendering (PNG)
  fileops.py                filename templating, move/copy, duplicates
  history.py                cv_history.csv
  processor.py              analyze() and finalize() - the pipeline
  gui/                      app.py (main), review_dialog.py, settings_window.py, common.py
tests/                      python -m unittest discover -s tests -v
```

## Extending
- **New file type:** add `@register(".ext")` function in `readers.py` returning `ReadResult(text)`.
- **New built-in field:** subclass `FieldExtractor` in `extractor.py`, decorate with `@builtin`,
  add `key: label` to `BUILTIN_FIELDS` in `config.py`.

## Tips & limits
- Name/title/company detection is heuristic (works best on conventional layouts). That's what the
  verification pop-up is for; tune keywords in Settings to improve accuracy.
- Phone numbers that look like year ranges (2018 – 2022) are ignored.
- Scanned-image quality matters: 200–300 DPI gives the best OCR.
