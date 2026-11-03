# Sheet Name Matcher — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**Sheet Name Matcher** is an offline, client-side browser application designed for reconciliation, deduplication, and fuzzy matching of individual names between two separate spreadsheets (Sheet A and Sheet B). It specifically addresses real-world data discrepancies including spelling errors, transpositions, swapped first/last name order, missing or extra words, and extensive Arabic orthographic spelling variants.

---

## 2. Exhaustive Feature Inventory

### 2.1 Spreadsheet Ingestion & Column Configuration
- **Dual Source Ingestion**: Side-by-side cards for Sheet A and Sheet B supporting Drag & Drop and native file picker inputs.
- **Broad File Format Compatibility**: Ingests Microsoft Excel workbooks (`.xlsx`, `.xls`) and Comma-Separated Values files (`.csv`) via an embedded build of SheetJS (`xlsx.js` v1.15.0).
- **Multi-Worksheet Navigation**: When a workbook contains multiple worksheets, a dropdown menu dynamically displays all sheet names.
- **Smart Column Auto-Detection (`guessKey`)**:
  - Automatically identifies candidate name columns by scanning headers with the regex `/name|full|اسم|الاسم|الإسم|nom|nombre/i`.
- **Multi-Column Composite Key Selection**:
  - Displays interactive toggle chips for every column in the sheet.
  - Users can select multiple chips (e.g. `First Name` + `Father Name` + `Surname`), which are automatically concatenated into a single unified search string.
- **Instant Data Preview**: Shows total row counts alongside a live preview of the first three rows.
- **Built-In Dual-Language Sample Data (`loadSample`)**: Launches with a preloaded sample dataset of English and Arabic names to demonstrate matching immediately.

### 2.2 Specialized Bilingual Normalization Engine (`norm`)
- **Arabic-Indic Digit Translation**: Automatically maps Eastern Arabic digits (`٠١٢٣٤٥٦٧٨٩`) to standard Western digits (`0123456789`).
- **Unicode NFKD Decompounding & Diacritic Stripping**:
  - Eliminates Latin combining accents (`[\u0300-\u036F]`).
  - Strips all Arabic harakat/tashkeel diacritics: Fathah, Dammah, Kasrah, Sukun, Shaddah, Tanween (`[\u064B-\u065F]`), Dagger Alef (`\u0670`), and Tatweel/Kashida (`\u0640`).
- **Arabic Orthographic Variant Unification**:
  - Normalizes all forms of Alef (`أ`, `إ`, `آ`, `ٱ`) to bare Alef (`ا`).
  - Maps Alef Maksura (`ى`) to Yaa (`ي`).
  - Maps Teh Marbuta (`ة`) to Heh (`ه`).
  - Maps Waw with Hamza (`ؤ`) to Waw (`و`).
  - Maps Yaa with Hamza (`ئ`) to Yaa (`ي`).
- **Punctuation Scrubbing & Case Normalization**:
  - Strips symbols, hyphens, and punctuation (`[^\p{L}\p{N}\s]`).
  - Collapses multiple whitespace characters and converts Latin text to lowercase.

### 2.3 Multi-Strategy Similarity Scoring Algorithm (`score`)
The scoring engine computes three distinct similarity metrics and selects the maximum:
1. **Levenshtein Distance Ratio (`r1`)**:
   - `ratio(a, b) = 1 - (levenshtein_distance / max_length)`.
   - Captures character-level typos and slight misspellings.
2. **Token Sort Ratio (`r2`)**:
   - Alphabetically sorts all individual words in the name before comparison (`a.sorted` vs. `b.sorted`).
   - Effectively yields a 100% score for names with swapped components (e.g. "Mohammed Ali Hassan" vs. "Hassan Mohamed Ali").
3. **Length-Weighted Token Overlap & Coverage Ratio (`r3`)**:
   - Compares every token in the shorter string against tokens in the longer string.
   - Weights matched tokens proportionally by character length.
   - Applies an asymmetric coverage penalty: `r3 = tf * (0.85 + 0.15 * (short_len / long_len))`.
   - Accurately matches partial names (e.g., three-part name vs. four-part name).
- **Final Composite Metric**: `score = Math.max(r1, r2, r3)`.

### 2.4 Time-Sliced Non-Blocking Execution Engine
- **Chunked Asynchronous Processing (`chunk`)**:
  - Computes pairwise matrix comparisons in 40ms time slices using `setTimeout`.
  - Prevents the browser tab from freezing or showing "unresponsive script" warnings even when comparing thousands of rows.
- **Top-N Candidate Collection**:
  - Configurable dropdown selector allowing users to retrieve the Top 1, 2, 3, or 5 closest matches for every candidate.
  - Automatically filters out candidate pairs scoring below a 40% similarity baseline.
- **Visual Progress Bar**: Displays an animated progress bar indicating matching percentage from 0% to 100%.

### 2.5 Reconciliation Ledger & Filtering Tools
- **Configurable Match Threshold Slider (`#thr`)**:
  - Slider ranging from 50% to 100% (default: 80%).
- **Three-Tier Classification Badges**:
  - **Strong Match (`s`)**: Similarity >= 90% (Green).
  - **Probable Match (`p`)**: Threshold <= Similarity < 90% (Amber / Review recommended).
  - **Weak / No Match (`w`)**: Similarity < Threshold (Red).
- **Interactive Metric Filter Pills**:
  - Instant filter buttons for: *All Names*, *Strong Matches*, *Probable Matches*, and *Weak/Unmatched*.
- **Live Search Filter**: Real-time search box to quickly locate specific names across either Sheet A or Sheet B.
- **Ledger Table Presentation**:
  - Row numbers referencing original Excel row numbers from Sheet A (`ai + 2`).
  - Name in Sheet A (supports auto-directional `dir="auto"` text).
  - Best Match in Sheet B, accompanied by smaller sub-rows showing alternate top-N candidates and their similarity percentages.
  - Graphical similarity progress bar and percentage value.
  - Status badge pill.
- **Virtual Pagination**: Displays 200 initial rows with a "Show more rows" button (+300 rows per click) to maintain DOM rendering performance.

### 2.6 Comprehensive 3-Sheet Excel Export (`download`)
Clicking "Download results (.xlsx)" generates a comprehensive multi-sheet workbook containing:
1. **"Matches" Sheet**:
   - Preserves all original metadata columns from Sheet A.
   - Best Match name from Sheet B.
   - Similarity Percentage.
   - Match Status (*Strong*, *Probable*, *Weak*, *None*).
   - Preserves all original metadata columns from Sheet B.
2. **"All candidates" Sheet**:
   - A complete ranking of every candidate match found for every name in Sheet A, including: Sheet A Name, Sheet A Excel Row, Rank (1, 2, 3...), Candidate Name, Candidate Excel Row, and Similarity %.
3. **"Unmatched in B" Sheet**:
   - Lists all entries from Sheet B that were never matched to any record in Sheet A above the threshold.

### 2.7 UI/UX & Theming
- **Theme Switcher**: One-click "Toggle theme" button switching between Dark and Light color palettes with CSS custom properties.
- **Responsive Layout**: Adapts from wide desktop ledger format to single-column tablet/mobile view.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```javascript
// SHEET NAME MATCHER PSEUDO-SCRIPT

function sheetNameMatcher(sheetA_data, sheetB_data, options) {
  // 1. Prepare and Normalize Datasets
  const listA = sheetA_data.rows.map(row => {
    const rawName = extractCompositeKey(row, sheetA_data.selectedColumns);
    const normalized = normalizeArabicAndLatin(rawName);
    const tokens = normalized.split(' ');
    const sorted = tokens.slice().sort().join(' ');
    return { raw: rawName, norm: normalized, tokens, sorted, originalRow: row };
  });

  const listB = sheetB_data.rows.map(row => {
    const rawName = extractCompositeKey(row, sheetB_data.selectedColumns);
    const normalized = normalizeArabicAndLatin(rawName);
    const tokens = normalized.split(' ');
    const sorted = tokens.slice().sort().join(' ');
    return { raw: rawName, norm: normalized, tokens, sorted, originalRow: row };
  });

  // 2. Perform Pairwise Scoring in Non-Blocking Time-Slices
  const results = [];
  
  for (let i = 0; i < listA.length; i++) {
    const itemA = listA[i];
    const candidates = [];
    
    if (itemA.norm) {
      for (let j = 0; j < listB.length; j++) {
        const itemB = listB[j];
        
        // Calculate similarity metrics
        const r1 = levenshteinRatio(itemA.norm, itemB.norm);
        const r2 = (itemA.sorted === itemB.sorted) ? 1.0 : levenshteinRatio(itemA.sorted, itemB.sorted);
        const r3 = calculateTokenOverlap(itemA.tokens, itemB.tokens);
        
        const finalScore = Math.max(r1, r2, r3);
        
        if (finalScore >= 0.40) {
          candidates.push({ bIndex: j, score: finalScore });
          candidates.sort((a, b) => b.score - a.score);
          if (candidates.length > options.topN) candidates.pop();
        }
      }
    }
    
    results.push({ aIndex: i, candidates });
  }

  // 3. Classify Results
  results.forEach(res => {
    const best = res.candidates[0];
    if (!best || best.score < options.threshold) {
      res.status = 'weak';
    } else if (best.score >= 0.90) {
      res.status = 'strong';
    } else {
      res.status = 'probable';
    }
  });

  // 4. Generate 3-Sheet Workbook
  const workbook = createWorkbook({
    MatchesSheet: buildMatchesSheet(results, sheetA_data, sheetB_data),
    AllCandidatesSheet: buildCandidatesSheet(results, listA, listB),
    UnmatchedInBSheet: buildUnmatchedSheet(results, listB, options.threshold)
  });

  saveExcelFile(workbook, "name-matching-results.xlsx");
}
```

---

## 4. UI Elements & Configuration Reference Table

| UI Element | Control Type | Default Value | Description |
|---|---|---|---|
| Sheet A / Sheet B Drop | Drag & Drop / Input | - | Ingests `.xlsx`, `.xls`, `.csv` spreadsheets |
| Worksheet Dropdown | Select Menu | Sheet 1 | Selects active sheet from multi-sheet workbooks |
| Column Chips | Toggle Buttons | Auto-detected | Selects one or multiple columns to concatenate |
| Match Threshold (`#thr`)| Range Slider | `80%` | Minimum similarity score (50% - 100%) |
| Candidates per name | Select Menu | `3` | Number of ranked candidates to keep (1, 2, 3, 5) |
| Run Matching (`#run`) | Action Button | - | Starts progressive matching execution |
| Filter Pills | Button Group | `All` | Filters view to All, Strong, Probable, or Weak |
| Search Filter (`#q`) | Search Input | `""` | Filters table rows by text substring |
| Download Results (`#dl`)| Action Button | - | Generates 3-sheet reconciliation workbook |
| Show more rows | Action Button | - | Increases visible DOM rows by 300 |
| Toggle Theme | Action Button | System Default | Toggles between Dark and Light themes |

