// citation + CURIE linkification helpers, shared between the in-app
// MarkdownAnswer / StructuredAnswer renderers and the PDF / Markdown
// exporters. previously these lived as duplicate copies in both
// components — extracted here so the PDF / Markdown exports linkify
// the explanation prose the SAME way the screen view does.

const PMID_RE = /^PMID:(\d+)$/i;
const PLOVER_EDGE_RE = /^PloverDB-edge:([0-9a-zA-Z_.-]+)$/;

// turns "[PMID:33487311, PMID:35319388]" into a comma-separated list
// of clickable markdown links, AND strips the surrounding square
// brackets when at least one item became a link.
//
// CommonMark cannot parse "[[a](u), [b](u)]" — the outer "[" starts
// a link-reference attempt that fails to find a matching "(url)"
// after the closing "]", so the whole span renders as literal text
// (this was the bug where PubMed URLs were leaking visibly into the
// rendered output). dropping the outer brackets when items got
// linkified gives us a clean comma-separated list of clickable PMIDs.
//
// PloverDB-edge ids render as styled-but-not-linked spans (backticks)
// since they're internal KG2c identifiers without a public URL.
export function linkifyCitations(text: string): string {
  return text.replace(/\[([^\]]+)\]/g, (whole, inner: string) => {
    const items = inner.split(/\s*,\s*/);
    const parts: string[] = [];
    let anyHit = false;
    for (const item of items) {
      const pmid = item.match(PMID_RE);
      const edge = item.match(PLOVER_EDGE_RE);
      if (pmid) {
        anyHit = true;
        parts.push(
          `[PMID:${pmid[1]}](https://pubmed.ncbi.nlm.nih.gov/${pmid[1]}/)`,
        );
      } else if (edge) {
        anyHit = true;
        parts.push(`\`PloverDB-edge:${edge[1]}\``);
      } else {
        parts.push(item);
      }
    }
    return anyHit ? parts.join(", ") : whole;
  });
}

// turns bare CURIEs in prose into bioregistry.io links. example:
//   "metformin (CHEBI:6801) treats type 2 diabetes (MONDO:0005148)"
// the function walks the text, skips spans that are already inside
// markdown link syntax `[text](url)` (otherwise we'd nest links into
// the PMID citations linkifyCitations already produced), and wraps
// every CURIE-looking token in the gaps.
export function linkifyCURIEs(text: string): string {
  // matches a single markdown link `[text](url)`. we collect their
  // ranges so the CURIE pass can skip over them.
  const MARKDOWN_LINK = /\[[^\]]*\]\([^)]*\)/g;
  // a CURIE is an uppercase-led prefix, a colon, and an identifier.
  // the {1,15} / {1,40} caps guard against pathological matches in
  // free text. PloverDB-edge has a dash in the prefix so it doesn't
  // match here — those are already wrapped in backticks above.
  const CURIE = /\b([A-Z][A-Za-z0-9.]{1,15}):([A-Za-z0-9_.\-]{1,40})\b/g;

  const linkSpans: Array<[number, number]> = [];
  for (const m of text.matchAll(MARKDOWN_LINK)) {
    if (m.index !== undefined) linkSpans.push([m.index, m.index + m[0].length]);
  }
  function isInsideExistingLink(pos: number): boolean {
    for (const [s, e] of linkSpans) {
      if (pos >= s && pos < e) return true;
    }
    return false;
  }

  return text.replace(CURIE, (whole, prefix: string, _local: string, offset: number) => {
    // PMID is already handled by linkifyCitations (inside square
    // brackets). don't double-process.
    if (prefix === "PMID") return whole;
    if (isInsideExistingLink(offset)) return whole;
    return `[${whole}](https://bioregistry.io/${encodeURIComponent(whole)})`;
  });
}
