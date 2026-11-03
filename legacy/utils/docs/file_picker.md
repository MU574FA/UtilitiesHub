# File Picker — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**File Picker** is a pure Python standard-library desktop utility designed for rapid, flexible batch retrieval and organization of files across deep directory hierarchies. Given an arbitrary list of target names (loaded from files, pasted from spreadsheets, or typed manually), it locates matching files regardless of their extension, resolves collisions, filters extensions, preserves or flattens folder structures, previews actions in a dry-run mode, and copies or moves matched items to a specified destination while cataloging missing files.

---

## 2. Exhaustive Feature Inventory

### 2.1 Input Data Ingestion & Normalization
- **Multi-Delimiter Text Parser (`parse_names`)**:
  - Automatically splits input text across newlines, commas, semicolons, and tabs using the regex `[\n,;\t]+`.
  - Cleans input tokens by stripping leading/trailing whitespace and quotes (`"` or `'`).
  - **Deduplication with Order Retention**: Deduplicates items case-insensitively while strictly preserving the user's original sequence.
  - Live Name Counter: Dynamically updates the label `X names` as the user types or pastes text.
- **Direct File Import**:
  - "Load from file..." button supporting `.txt`, `.csv`, `.lst`, or all files (`*.*`).
  - Decodes with `utf-8-sig` to automatically handle BOM headers from Microsoft Excel.
- **Clipboard Paste**: One-click "Paste" button to quickly ingest columns copied from spreadsheets.
- **One-Click Clear**: Instantly empties the text area.

### 2.2 Matching Engine & Modes
The application offers four distinct string-matching modes:
1. **Exact Name, Any Extension (`exact`)**:
   - Indexes files by splitting filenames on periods and registering every sub-stem.
   - For example, an archive named `document.tar.gz` is indexed and matched by `document.tar.gz`, `document.tar`, and `document`.
   - Allows finding files when only the base name is known without needing to specify the extension.
2. **Starts With (`starts`)**:
   - Evaluates whether the filename begins with the candidate token (prefix search).
3. **Contains (`contains`)**:
   - Matches if the candidate substring appears anywhere within the filename.
4. **Regular Expression (`regex`)**:
   - Full regular expression pattern matching.
   - Validates regex syntax across all input names before initiating operations; displays an error dialog if any pattern is invalid.
- **Case Sensitivity Option**: Global toggle for case-sensitive vs. case-insensitive matching.

### 2.3 Traversal & Extension Filtering
- **Recursive Subfolder Scanning**:
  - Toggle: "Include subfolders" uses `Path.rglob("*")` when enabled, or flat `Path.glob("*")` when disabled.
- **Folder Structure Preservation**:
  - Toggle: "Keep subfolder structure in output" replicates the relative parent path tree inside the destination folder.
  - When unchecked, all matched files are flattened into the root of the output directory.
- **Loop & Self-Copy Prevention**:
  - Detects if the output folder is located inside the source folder and automatically skips files already present in the destination.
  - Prevents source and destination from being identical unless structure preservation is enabled.
- **Extension Whitelist**:
  - Accepts comma-delimited extensions (e.g. `jpg, png, pdf`).
  - Normalizes leading dots and casing. Leaving the field blank matches all extensions.

### 2.4 File Operations & Collision Policies
- **Execution Actions**:
  - **Copy**: Safe duplication leaving source files intact.
  - **Move**: Transfers files out of source folder (requires explicit user confirmation dialog before running).
- **Conflict Handling Policies**:
  - **Rename (`unique_path`)**: Generates incremental, collision-free names (e.g. `report.pdf` -> `report (1).pdf` -> `report (2).pdf`).
  - **Skip**: Ignores files that already exist in destination and logs a warning.
  - **Overwrite**: Replaces the destination file.
- **Automatic Directory Creation**: Automatically creates all required parent folders (`mkdir(parents=True, exist_ok=True)`).

### 2.5 Multi-Threaded Execution, Preview & Feedback
- **Background Worker Thread**: All filesystem scanning, matching, and I/O run in a separate `threading.Thread` via a message queue (`queue.Queue`), keeping the Tkinter UI completely responsive.
- **Safe Dry-Run Preview Mode**:
  - The "Preview" button executes the full search and collision logic without making any disk modifications.
  - Logs every prospective action with the prefix `WOULD COPY` or `WOULD MOVE`.
- **Cancellation**: "Cancel" button powered by `threading.Event` allows stopping long batch jobs mid-stream.
- **Color-Coded Audit Log**:
  - `ok` (Green): Successful transfers or preview matches.
  - `warn` (Orange): Missing names, skips, or cancellation alerts.
  - `err` (Red): Filesystem permissions or I/O errors.
- **Deterministic Progress Bar**: Smoothly advances from `1` to `Total Names` with live status text (`Processing X/Y names...`).
- **Missing Names Exporter**:
  - When names cannot be found, the "Save missing..." button activates.
  - Exports all missing entries to `missing_names.txt` or a user-chosen path.

### 2.6 Persistence & Environment
- **Zero Third-Party Dependencies**: Runs on standard Python 3 with built-in `tkinter`.
- **Automatic Settings Persistence**: Stores all paths, checkboxes, and selections in `~/.file_picker_settings.json` upon exit and restores them on launch.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```python
# FILE PICKER PSEUDO-SCRIPT

def run_file_picker(input_dir, output_dir, names_raw, options, dry_run=False):
    # 1. Parse and deduplicate search terms
    names = parse_names(names_raw) # split on [\n,;\t]+, strip quotes, preserve order
    
    # 2. Collect eligible files from input directory
    files = []
    iterator = input_dir.rglob("*") if options.subfolders else input_dir.glob("*")
    for path in iterator:
        if not path.is_file():
            continue
        if is_inside_output_dir(path, output_dir):
            continue
        if options.exts and path.suffix.lower() not in options.exts:
            continue
        files.append(path)
        
    # 3. Build index for exact matching (multi-extension aware)
    norm = (lambda s: s) if options.case_sensitive else (lambda s: s.lower())
    index = {}
    if options.mode == "exact":
        for path in files:
            parts = path.name.split(".")
            for i in range(len(parts), 0, -1):
                key = norm(".".join(parts[:i]))
                index.setdefault(key, []).append(path)
                
    # 4. Search and Process Each Name
    missing = []
    used_files = set()
    
    for name in names:
        key = norm(name)
        if options.mode == "exact":
            hits = index.get(key, [])
        elif options.mode == "starts":
            hits = [p for p in files if norm(p.name).startswith(key)]
        elif options.mode == "contains":
            hits = [p for p in files if key in norm(p.name)]
        elif options.mode == "regex":
            rx = re.compile(name, 0 if options.case_sensitive else re.I)
            hits = [p for p in files if rx.search(p.name)]
            
        if not hits:
            missing.append(name)
            log(f"NOT FOUND: {name}", level="warn")
            continue
            
        for path in hits:
            if path in used_files:
                continue
            used_files.add(path)
            
            # Determine destination path
            rel_dir = path.relative_to(input_dir).parent if options.keep_subfolders else Path()
            target_path = output_dir / rel_dir / path.name
            
            # Resolve conflicts
            if target_path.exists():
                if options.conflict == "skip":
                    log(f"SKIP (exists): {path.name}", level="warn")
                    continue
                elif options.conflict == "rename":
                    target_path = unique_path(target_path) # e.g. file (1).ext
                    
            # Execute or Preview
            action_verb = ("WOULD " if dry_run else "") + options.action.upper()
            if not dry_run:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                if options.action == "move":
                    shutil.move(str(path), str(target_path))
                else:
                    shutil.copy2(str(path), str(target_path))
                    
            log(f"{action_verb}: {path.name} -> {target_path}", level="ok")
            
    return missing
```

---

## 4. UI Elements & Options Reference Table

| UI Component | Control Type | Default | Options / Values |
|---|---|---|---|
| `Input folder` | Entry + Browse button | `""` | Directory containing source files |
| `Output folder`| Entry + Browse button | `""` | Destination directory |
| `Names` | Text area + Scrollbar | `""` | List of file stems or names to find |
| `Match mode` | Radio group | `exact` | `Exact name, any extension`, `Starts with`, `Contains`, `Regex` |
| `Action` | Radio group | `copy` | `Copy` or `Move` |
| `If file exists`| Combobox | `rename` | `rename` (append `(1)`), `skip`, `overwrite` |
| `Only extensions`| Entry | `""` | Whitelist (e.g. `jpg, png, pdf`; empty = all) |
| `Include subfolders`| Checkbutton | `True` | Recursive directory traversal |
| `Keep subfolder structure`| Checkbutton | `False` | Replicates subfolder tree in output |
| `Case sensitive` | Checkbutton | `False` | Case sensitivity toggle |
| `Preview` | Button | - | Dry-run simulation |
| `Run` | Button | - | Executes live copy/move operation |
| `Cancel` | Button | - | Halts worker thread |
| `Save missing...`| Button | - | Exports unmatched names to `.txt` |

