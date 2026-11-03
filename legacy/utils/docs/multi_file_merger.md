# Multi File Merger (Merge Desk) — Complete Features, Functionalities & Architecture Specification

## 1. Executive Summary & Purpose
**Multi File Merger** (internally branded as **Merge Desk**) is an offline-first single-page web application that merges heterogeneous document types—specifically Adobe PDFs, Microsoft Word (`.docx`) files, and various image formats (`.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`, `.bmp`, `.avif`)—into a unified, publication-ready PDF. It executes entirely client-side inside the user's browser without uploading documents to external servers.

---

## 2. Exhaustive Feature Inventory

### 2.1 File Ingestion & Format Detection
- **Multi-Format Drag-and-Drop**: Interactive dropzone with visual drag-over feedback (`.drop.over`) and native file input (`multiple`).
- **Comprehensive Format Support**:
  - **PDF Documents**: Direct binary inspection via `pdf-lib`.
  - **Images**: `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif`, `.bmp`, `.avif`.
  - **Word Documents**: Modern Microsoft Word `.docx` documents.
- **Unsupported Format Interception**: Detects legacy `.doc` files and shows a clear user warning explaining that old binary Word files must first be saved as `.docx`.
- **Protected PDF & Corrupt File Guard**: Traps password-protected, encrypted, or corrupted files immediately upon file selection and displays descriptive error notices.

### 2.2 Ordered File Workspace & Reordering Controls
- **Visual File Queue**:
  - Renders each file as a card containing an icon or visual thumbnail, filename, file position index (`X of Y`), file size (formatted as B, KB, or MB), and page count (for PDFs).
- **Interactive Thumbnails**:
  - Images render live thumbnail previews generated from local Object URLs (`URL.createObjectURL`).
  - PDFs and DOCX files render colored format badges.
- **Reordering Controls**:
  - **Move Up** and **Move Down** buttons for each item to adjust position in the merged sequence.
  - **Remove** button to delete an item and revoke its associated memory resources.
  - Dynamic button disabling: "Move Up" disabled on the first item; "Move Down" disabled on the last; all buttons disabled during an active merge.
- **Summary Metrics Counter**: Displays real-time counts of total files, calculated total output pages, and Word document counts.

### 2.3 Image Processing & Formatting Modes
- **Configurable Photo Layout (`#imgmode`)**:
  - **Fit on an A4 Page (`a4`)**:
    - Automatically detects photo orientation (landscape vs. portrait).
    - Sets page dimensions to standard A4 (595.28 x 841.89 pt).
    - Computes proportional scaling to fit inside a 28pt margin while maintaining aspect ratio and centering the photo.
  - **Page Matches the Photo (`own`)**:
    - Sets PDF page dimensions to exactly match the image aspect ratio (`width * 0.75` pt by `height * 0.75` pt).
- **Advanced Rasterization & Orientation Correction (`rasterize`)**:
  - Uses `createImageBitmap` with `{ imageOrientation: 'from-image' }` to automatically correct EXIF rotation tags from smartphone cameras.
  - **Resolution Safeguard**: Caps image dimensions at a maximum of 2600px along the longest edge to prevent out-of-memory crashes on mobile devices or browsers.
  - Background Fill: Fills opaque white backgrounds for non-PNG formats; preserves transparency for PNGs.
  - Encoders: Encodes compressed images as 92% JPEG or lossless PNG.

### 2.4 Word Document (`.docx`) Rendering Engine (`addDocx`)
- **Client-Side Document Parsing**: Uses `mammoth.browser.js` to parse `.docx` XML structures into semantic HTML without server round-trips.
- **Bidirectional & RTL Language Support**:
  - Traverses converted headings (`h1`-`h6`), paragraphs (`p`), list items (`li`), and table cells (`td`, `th`), injecting `dir="auto"`.
  - Accurately renders Arabic, Hebrew, and other right-to-left scripts alongside Latin text.
- **Document Image Preloading**: Asynchronously awaits decoding of all images embedded in the Word document.
- **Virtual Page Slicing & Rasterization**:
  - Projects the HTML layout into a calibrated 642px-wide print container.
  - Calculates virtual page height chunks (971px slices).
  - Uses `html2canvas` at 1.6x scale factor with `#ffffff` background to rasterize each page slice into high-resolution canvas elements.
  - Encodes canvases into JPEG images and embeds them sequentially onto A4 pages within the PDF.

### 2.5 Assembly, PDF Metadata & Progress Engine
- **Non-Blocking Assembly Loop**: Uses asynchronous time slicing (`tick()`) between pages so the browser UI never freezes during large merges.
- **Real-Time Progress Bar**:
  - Displays a percentage-filled progress bar (`#fill`) with dynamic status text (e.g. `File 2 of 5 · Contract.docx (page 1 of 3)`).
- **PDF Metadata Stamping**:
  - Customizes output PDF title based on the filename.
  - Sets the PDF Producer metadata tag to `Merge Desk`.
- **Filename Sanitization (`safeName`)**:
  - Strips illegal characters (`[\\/:*?"<>|\u0000-\u001f]`).
  - Ensures a valid `.pdf` extension is appended.

### 2.6 Dual Export Mechanism & Memory Hygiene
- **Environment-Aware Export**:
  - Checks for the Claude Desktop / Artifacts download bridge (`window.claude.use('downloads')`) and calls `dl.save()` if available.
  - Standard Browser Fallback: Generates a temporary Blob URL (`application/pdf`), injects an invisible download link, triggers the browser download manager, and schedules object URL revocation after 10 seconds.
- **Memory Hygiene (`revoke`)**:
  - Immediately revokes blob object URLs upon file removal or when clicking "Start over".

### 2.7 UI/UX & Responsive Layout
- **Theme Support**: Adaptive Dark and Light themes responding automatically to system preferences (`prefers-color-scheme`) or explicit `data-theme` overrides.
- **Sticky Actions Bar**: Pinned mobile-friendly bottom bar with file count, page total, and primary "Merge" button.
- **Typography & Accessibility**: Styled with IBM Plex Sans, IBM Plex Mono, and Bricolage Grotesque fonts; adheres to `prefers-reduced-motion`.

---

## 3. End-to-End Pseudo-Script / Architecture Flow

```javascript
// MULTI FILE MERGER (MERGE DESK) PSEUDO-SCRIPT

async function mergeDeskWorkflow(fileList, options) {
  // 1. Initialize PDF Document
  const pdfDoc = await PDFLib.PDFDocument.create();
  
  // 2. Iterate Through User-Ordered Files
  for (let i = 0; i < fileList.length; i++) {
    const item = fileList[i];
    updateProgressBar(i, fileList.length, item.name);
    
    // CASE A: Standard PDF
    if (item.kind === 'pdf') {
      const srcDoc = await PDFLib.PDFDocument.load(await item.file.arrayBuffer());
      const pageIndices = srcDoc.getPageIndices();
      const copiedPages = await pdfDoc.copyPages(srcDoc, pageIndices);
      copiedPages.forEach(page => pdfDoc.addPage(page));
    }
    
    // CASE B: Image File
    else if (item.kind === 'img') {
      // Correct EXIF rotation & limit max dimension to 2600px
      const raster = await rasterizeImage(item.file, { maxDim: 2600 });
      const imgEmbed = raster.isPng 
        ? await pdfDoc.embedPng(raster.bytes) 
        : await pdfDoc.embedJpg(raster.bytes);
        
      if (options.imgMode === 'own') {
        const page = pdfDoc.addPage([raster.width * 0.75, raster.height * 0.75]);
        page.drawImage(imgEmbed, { x: 0, y: 0, width: page.getWidth(), height: page.getHeight() });
      } else {
        // Fit onto A4 with 28pt margins
        const isLandscape = raster.width > raster.height;
        const pageW = isLandscape ? 841.89 : 595.28;
        const pageH = isLandscape ? 595.28 : 841.89;
        const page = pdfDoc.addPage([pageW, pageH]);
        const scale = Math.min((pageW - 56) / raster.width, (pageH - 56) / raster.height);
        const drawW = raster.width * scale;
        const drawH = raster.height * scale;
        page.drawImage(imgEmbed, {
          x: (pageW - drawW) / 2,
          y: (pageH - drawH) / 2,
          width: drawW,
          height: drawH
        });
      }
    }
    
    // CASE C: Word Document (.docx)
    else if (item.kind === 'docx') {
      const { value: html } = await mammoth.convertToHtml({ arrayBuffer: await item.file.arrayBuffer() });
      const container = createHiddenHostElement(html); // set dir="auto" on all block elements
      
      const totalHeight = container.scrollHeight;
      const pageHeight = 971; // px
      const numSlices = Math.ceil(totalHeight / pageHeight);
      
      for (let s = 0; s < numSlices; s++) {
        const sliceCanvas = await html2canvas(container, {
          y: s * pageHeight,
          height: Math.min(pageHeight, totalHeight - s * pageHeight),
          width: 642,
          scale: 1.6
        });
        const sliceJpg = await canvasToJpegBlob(sliceCanvas);
        const embeddedSlice = await pdfDoc.embedJpg(await sliceJpg.arrayBuffer());
        
        const a4Page = pdfDoc.addPage([595.28, 841.89]);
        a4Page.drawImage(embeddedSlice, {
          x: 57,
          y: 841.89 - 57 - (sliceCanvas.height / 1.6) * 0.75,
          width: 642 * 0.75,
          height: (sliceCanvas.height / 1.6) * 0.75
        });
      }
      container.remove();
    }
  }
  
  // 3. Stamp Metadata & Save
  pdfDoc.setProducer('Merge Desk');
  pdfDoc.setTitle(sanitizeFilename(options.outName));
  const mergedPdfBytes = await pdfDoc.save();
  
  // 4. Trigger Download
  downloadBlob(new Blob([mergedPdfBytes], { type: 'application/pdf' }), options.outName);
}
```

---

## 4. UI Elements & Configuration Reference Table

| UI Component | Type | Default Value | Description |
|---|---|---|---|
| Drop Target (`#drop`) | Drag & Drop / File Input | Accepts PDF, DOCX, Images | Add files to workspace |
| Item Move Up / Down | Icon Buttons | - | Shifts file order in merge sequence |
| Item Remove | Icon Button | - | Deletes file from workspace and revokes URL |
| Photos in the PDF (`#imgmode`)| Dropdown | `Fit on an A4 page` (`a4`) | `Fit on an A4 page` or `Page matches the photo` (`own`) |
| File name (`#outname`) | Text Input | `merged.pdf` | Desired name for compiled output file |
| Merge Button (`#go`) | Action Button | - | Starts progressive compilation |
| Progress Bar (`#prog`) | Progress Bar + Label | - | Visual progress of file/page rendering |
| Save PDF | Action Button | - | Downloads compiled output PDF |
| Start over | Action Button | - | Resets workspace and frees memory |
| Theme Toggle | Auto / Media Query | System Theme | Responsive Dark and Light styling |

