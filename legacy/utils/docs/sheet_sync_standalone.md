# Sheet Sync Standalone — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**Sheet Sync Standalone** is a lightweight, zero-dependency, client-side web application designed to merge, update, and reconcile master tabular datasets (e.g., an existing "Old" Excel sheet) against newer versions ("New" sheet). It aligns records using a unique key column, identifies modified cells, adds brand-new rows, handles blank cells conditionally, and exports an updated workbook along with a forensic audit trail of every changed cell. Remarkably, the entire application—including raw ZIP decompression, Open Packaging Conventions XML parsing, CSV parsing, CRC-32 hashing, and XLSX generation—is built from scratch in pure vanilla JavaScript without external libraries.

---

## 2. Exhaustive Feature Inventory

### 2.1 Pure Vanilla In-Memory XLSX & CSV Engine
- **Zero-Dependency Architecture**: No SheetJS, no external CDN dependencies, and no backend servers. Everything runs locally in the browser using standard web APIs.
- **Native ZIP Decompressor (`unzip`)**:
  - Traverses raw binary ArrayBuffers to locate the End of Central Directory (`0x06054B50`) and Central Directory file headers (`0x02014B50`).
  - Utilizes the browser's native `DecompressionStream("deflate-raw")` to decompress raw deflate streams into UTF-8 XML files.
- **Full OpenXML SpreadsheetML Parser (`read`)**:
  - Parses workbook relationships (`xl/_rels/workbook.xml.rels`) and sheet mappings (`xl/workbook.xml`).
  - Resolves shared string tables (`xl/sharedStrings.xml`), skipping phonetic guide runs (`rPh`).
  - Analyzes workbook styles (`xl/styles.xml`) and custom number formatting rules (`numFmt` / `cellXfs`) to identify date fields and convert Excel numerical serial timestamps into human-readable ISO dates (`YYYY-MM-DD`).
  - Extracts cell coordinate references, supporting numeric values, boolean flags, inline strings (`inlineStr`), shared strings (`s`), string expressions (`str`), and error types (`e`).
  - Pads empty cells using a dense matrix transformer (`dense`) to guarantee rectangular data arrays.
- **Robust Delimiter-Detecting CSV Parser (`readCSV`)**:
  - Automatically detects delimiter characters (`,`, `;`, or `\t`) based on header frequency.
  - Accommodates UTF-8 Byte Order Marks (`0xFEFF`).
  - Properly parses multiline quoted fields and escaped quotation marks (`""`).
- **Complete In-Memory OpenXML XLSX Writer (`write`)**:
  - Custom CRC-32 checksum calculation table (`crcT`).
  - Dynamically builds the Open Packaging Conventions (OPC) directory structure:
    - `[Content_Types].xml`
    - `_rels/.rels`
    - `xl/workbook.xml`
    - `xl/_rels/workbook.xml.rels`
    - `xl/worksheets/sheetN.xml`
  - Auto-calculates column widths based on maximum character length per column (clamped between 8 and 50).
  - Automatically injects frozen header panes (`<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>`) into output worksheets.
  - Assembles and returns valid ZIP-compressed Excel workbooks as `Blob` objects.

### 2.2 Dual File Ingestion & Sheet Setup
- **Interactive File Dropzones**: Dedicated drop targets for the "Old" master file and the "New" updated file.
- **Worksheet Selectors**: Dropdown menus to choose specific sheets from multi-sheet workbooks.
- **Header Row Selectors (`hOld`, `hNew`)**:
  - Selectable header row index (Rows 1 to 10) for both files to accommodate files with title blocks, metadata banners, or leading blank lines.
  - Automatically deduplicates identical header names (e.g. `Status` -> `Status (2)`).

### 2.3 Record Matching & Key Normalization
- **Common Key Column Dropdown**: Automatically populates with column names shared between both sheets (e.g. `ID`, `Employee_Number`, `SKU`, `Email`).
- **Whitespace & Case Normalization (`nk`)**:
  - Checkbox: "Ignore upper/lower case and extra spaces in the unique value".
  - Trims, converts to lowercase, and collapses multiple whitespace runs to avoid match failures caused by subtle formatting differences.
- **Key Quality & Collision Warnings (`checkKey`)**:
  - Real-time diagnostic alerts highlighting duplicate key values in either file (advises that the first occurrence is matched).
  - Alerts when rows contain empty key values (advises that empty keys are ignored).

### 2.4 Column Selection & Update Rules
- **Interactive Column Checkboxes**:
  - Lists every shared column available to update.
  - One-click "Select all" and "Clear" helper links.
- **Configurable Synchronization Rules**:
  1. **Add New Rows (`addNew`)**: Adds rows found exclusively in the new file to the master sheet.
  2. **Preserve Old on Blank (`skipBlank`)**: Prevents empty cells in the new sheet from overwriting existing data in the old sheet.
  3. **Schema Expansion (`addCols`)**: Dynamically appends brand-new columns that exist only in the new file to the master table.
  4. **Status Column (`status`)**: Appends a `Sync status` column to the output table marking each row as `Updated`, `Added`, `Unchanged`, or `Not in new file`.

### 2.5 Live Audit Diff & Change Log Table
- **Summary Metrics Dashboard**:
  - Displays instant counts of **Updated rows**, **New rows added**, **Unchanged rows**, and total **Cells changed**.
- **Interactive Diff Table**:
  - Lists the Unique Key, Column Name, Old Value (rendered with strikethrough styling and muted color), and New Value (highlighted in soft gold).
  - Visual badges for newly inserted rows (`New row added` in soft green).
  - Virtual display cap: Renders the first 300 changes in the browser for peak performance, while guaranteeing 100% of changes are included in the downloadable workbook.

### 2.6 Dual Export Options
- **Download Updated Workbook (`#dl`)**:
  - Exports a complete Excel file containing:
    1. **"Updated" Sheet**: The full synchronized dataset with updated values, appended columns, new rows, frozen top header pane, and the `Sync status` column.
    2. **"Change log" Sheet**: An audit tab detailing Key, Column, Change Type (*Updated*, *Added*), Old Value, and New Value.
- **Download Change Log Only (`#dlLog`)**:
  - Generates a lightweight standalone spreadsheet containing only the audit trail sheet for compliance or review.

### 2.7 UI/UX & Offline Security
- **100% Client-Side Privacy**: Data never leaves the user's browser, making it safe for confidential payroll, employee, or financial data.
- **Responsive Dark/Light Theme**: CSS custom properties adapting dynamically to system theme settings or dark mode media queries.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```javascript
// SHEET SYNC STANDALONE PSEUDO-SCRIPT

async function runSheetSync(oldFile, newFile, config) {
  // 1. Parse Input Files (pure JS unzip & OpenXML parsing)
  const oldWorkbook = isCSV(oldFile.name) ? parseCSV(await oldFile.text()) : await parseXLSX(await oldFile.arrayBuffer());
  const newWorkbook = isCSV(newFile.name) ? parseCSV(await newFile.text()) : await parseXLSX(await newFile.arrayBuffer());

  const oldTable = extractTable(oldWorkbook, config.oldSheetIndex, config.oldHeaderRow);
  const newTable = extractTable(newWorkbook, config.newSheetIndex, config.newHeaderRow);

  // 2. Index the New Table by Unique Key
  const newIndex = new Map();
  newTable.rows.forEach(row => {
    const key = normalizeKey(row[config.keyColumn], config.ignoreCaseAndSpaces);
    if (key && !newIndex.has(key)) {
      newIndex.set(key, row);
    }
  });

  // 3. Process Master (Old) Rows
  const updatedRows = [];
  const changeLog = [];
  const matchedNewKeys = new Set();
  const extraColumns = config.addNewColumns 
    ? newTable.headers.filter(h => !oldTable.headers.includes(h))
    : [];

  const finalHeaders = [...oldTable.headers, ...extraColumns];

  oldTable.rows.forEach(oldRow => {
    const rowCopy = { ...oldRow };
    extraColumns.forEach(col => rowCopy[col] = "");
    
    const key = normalizeKey(oldRow[config.keyColumn], config.ignoreCaseAndSpaces);
    const newRow = newIndex.get(key);
    let rowHasChanged = false;

    if (newRow) {
      matchedNewKeys.add(key);
      const columnsToInspect = [...config.selectedColumns, ...extraColumns];

      columnsToInspect.forEach(col => {
        const oldVal = rowCopy[col];
        const newVal = newRow[col];

        if (newVal === undefined) return;
        if (config.keepOldOnBlank && normalize(newVal) === "") return;

        if (normalize(newVal) !== normalize(oldVal)) {
          changeLog.push({
            key: oldRow[config.keyColumn],
            column: col,
            change: "Updated",
            oldVal: oldVal,
            newVal: newVal
          });
          rowCopy[col] = newVal;
          rowHasChanged = true;
        }
      });

      rowCopy["Sync status"] = rowHasChanged ? "Updated" : "Unchanged";
    } else {
      rowCopy["Sync status"] = "Not in new file";
    }

    updatedRows.push(rowCopy);
  });

  // 4. Append New Rows
  if (config.addNewRows) {
    newTable.rows.forEach(newRow => {
      const key = normalizeKey(newRow[config.keyColumn], config.ignoreCaseAndSpaces);
      if (key && !matchedNewKeys.has(key) && newIndex.get(key) === newRow) {
        const addedRow = {};
        finalHeaders.forEach(h => addedRow[h] = newRow[h] !== undefined ? newRow[h] : "");
        addedRow["Sync status"] = "Added";
        updatedRows.push(addedRow);

        changeLog.push({
          key: newRow[config.keyColumn],
          column: "(all columns)",
          change: "Added",
          oldVal: "",
          newVal: ""
        });
      }
    });
  }

  // 5. Build and Download Updated Excel File
  const sheets = [
    { name: "Updated", headers: finalHeaders, rows: updatedRows },
    { name: "Change log", rows: changeLog }
  ];

  const xlsxBlob = generateXLSXPackage(sheets);
  triggerBrowserDownload(xlsxBlob, "sheet_updated.xlsx");
}
```

---

## 4. UI Elements & Configuration Reference Table

| UI Element | Type | Default Value | Description |
|---|---|---|---|
| Old File / New File Drop | Drag & Drop / Input | - | Loads `.xlsx` or `.csv` files |
| Old / New Sheet Picker | Select Menu | Sheet 1 | Active worksheet selector |
| Old / New Header Row | Select Menu | Row 1 | Designates row index (1 to 10) of header titles |
| Unique Column (`#key`) | Select Menu | Auto | Primary key column used for alignment |
| Case/Space Normalization (`#ci`)| Checkbox | `Checked` | Collapses spaces and converts key to lowercase |
| Columns to Update | Checkbox Grid | `All Checked` | Selectable columns to synchronize |
| Select all / Clear | Helper Links | - | Quickly checks or unchecks all column boxes |
| Add new rows (`#addNew`) | Checkbox | `Checked` | Inserts rows present only in the new file |
| Keep old on blank (`#skipBlank`)| Checkbox | `Checked` | Empty new cells will not clear old values |
| Add new columns (`#addCols`)| Checkbox | `Unchecked` | Expands table schema with novel columns |
| Sync status column (`#status`)| Checkbox | `Checked` | Adds column with Updated/Added/Unchanged tags |
| Preview changes (`#run`) | Action Button | - | Executes diff engine and populates change table |
| Download updated workbook | Action Button | - | Downloads multi-sheet `.xlsx` with updated data |
| Download change log only | Action Button | - | Downloads standalone change log spreadsheet |

