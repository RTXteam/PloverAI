// citation + CURIE linkification helpers, shared between the in-app
// MarkdownAnswer renderer and the PDF / Markdown exporters, so the
// exports linkify the explanation prose the SAME way the screen view
// does.

const PMID_RE = /^PMID:(\d+)$/i;
// the lookup condition's explainer cites facts by edge id.
const EDGE_RE = /^edge:([^\s,\]]+)$/;
// ARAX's explainer cites the facts of its reasoning graph as [F3],
// [F3, F7], and sometimes as a range [F1–F10].
const FACT_RE = /^F\d+(?:\s*[-–—]\s*F?\d+)?$/;
const FACT_GROUP_RE = /^F\d+(?:\|F\d+)+$/;

// an inline code span holding a citation group, e.g.
//   `[PMID:16799074, PMID:18408388]`
// some models wrap their citation groups in backticks. CommonMark then
// treats the group as a code span, so nothing inside it can ever become
// a link — and because linkifyCitations expands the contents anyway,
// the raw "[PMID:x](https://…)" markdown leaks into the rendered page.
const BACKTICKED_CITATION = /`(\[PMID[^`\n]*)`/gi;

// strips the backticks off citation groups so the expansion passes
// below can turn them into real links. deliberately narrow: it only
// unwraps spans whose content starts with "[PMID", so the `edge:…`
// spans that linkifyCitations itself emits (and any other code span in
// the prose) stay code.
//
// this runs at display time, so historical stored runs are fixed on
// redisplay without touching the saved pipeline output.
export function unwrapCitationCodeSpans(text: string): string {
  return text.replace(BACKTICKED_CITATION, (_whole, inner: string) => inner);
}

// turns "[PMID:33487311, PMID:35319388]" into a list of clickable
// markdown links, AND strips the surrounding square brackets when at
// least one item became a link.
//
// CommonMark cannot parse "[[a](u), [b](u)]" — the outer "[" starts
// a link-reference attempt that fails to find a matching "(url)"
// after the closing "]", so the whole span renders as literal text
// (this was the bug where PubMed URLs were leaking visibly into the
// rendered output). dropping the outer brackets when items got
// linkified gives us a clean list of clickable PMIDs.
//
// two adjacent PMID links are joined by a bare space rather than ", "
// — they render as chips (see the `a` override in MarkdownAnswer),
// and a comma between two pills reads as noise. the space is kept
// (rather than nothing) so the row still has line-break opportunities
// and long groups wrap.
//
// edge ids render as styled-but-not-linked spans (backticks) since
// they're internal identifiers without a public URL.
// fact citations beyond this many in one bracket fold into a single
// group chip ("F1 +8"), whose card lists them, instead of a row of chips.
const MAX_FACT_CHIPS = 3;

// four or more single-fact chips in a row ("[F1] [F3], [F5] [F9]", after
// expansion) fold the same way as one long bracket does.
const FACT_CHIP_RUN = /`F\d+`(?:[,\s]+`F\d+`){3,}/g;

export function linkifyCitations(text: string): string {
  return foldFactRuns(expandCitations(text));
}

function foldFactRuns(text: string): string {
  return text.replace(FACT_CHIP_RUN, (run) => `\`${(run.match(/F\d+/g) ?? []).join("|")}\``);
}

function expandCitations(text: string): string {
  return unwrapCitationCodeSpans(text).replace(/\[([^\]]+)\]/g, (whole, inner: string) => {
    let items = inner.split(/\s*,\s*/);
    const facts = items.filter((item) => FACT_RE.test(item));
    if (facts.length > MAX_FACT_CHIPS) {
      items = [facts.join("|"), ...items.filter((item) => !FACT_RE.test(item))];
    }
    const parts: string[] = [];
    // parallel to `parts`: true where that part renders as a chip (a
    // PubMed link or a fact citation); two chips are joined by a space.
    const isChip: boolean[] = [];
    let anyHit = false;
    for (const item of items) {
      const pmid = item.match(PMID_RE);
      const edge = item.match(EDGE_RE);
      const fact = FACT_RE.test(item) || FACT_GROUP_RE.test(item);
      if (pmid) {
        anyHit = true;
        parts.push(
          `[PMID:${pmid[1]}](https://pubmed.ncbi.nlm.nih.gov/${pmid[1]}/)`,
        );
        isChip.push(true);
      } else if (edge) {
        anyHit = true;
        parts.push(`\`edge:${edge[1]}\``);
        isChip.push(false);
      } else if (fact) {
        // a code span, which MarkdownAnswer turns into a citation chip
        // when it is given renderFact (the ARAX reasoning view).
        anyHit = true;
        parts.push(`\`${item}\``);
        isChip.push(true);
      } else {
        parts.push(item);
        isChip.push(false);
      }
    }
    if (!anyHit) return whole;
    let out = parts[0];
    for (let i = 1; i < parts.length; i++) {
      out += (isChip[i - 1] && isChip[i] ? " " : ", ") + parts[i];
    }
    return out;
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
  // free text. edge ids are already wrapped in backticks above, and
  // "edge" is lower-case, so they don't match here.
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
