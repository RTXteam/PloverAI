"use client";

// renders the pipeline's explanation paragraph as markdown with GFM
// support, AND turns three citation styles into clickable links:
//   [PMID:NNNNNN]              → pubmed.ncbi.nlm.nih.gov/NNNNNN
//   [PloverDB-edge:NNNNNN]     → inline code, no URL (internal id)
//   bare CURIEs in prose       → bioregistry.io resolver
// (e.g. CHEBI:6801, MONDO:0005148, NCBIGene:1080, GO:0006695)
// the LLM is told to emit plain text with bracketed citations and to
// mention CURIEs alongside entity labels, so we post-process the text
// into markdown links before handing it to react-markdown.

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { linkifyCitations, linkifyCURIEs } from "@/lib/linkify";

type Props = { text: string };

export function MarkdownAnswer({ text }: Props) {
  const enhanced = linkifyCURIEs(linkifyCitations(text));
  return (
    <article className="prose prose-zinc dark:prose-invert prose-sm sm:prose-base max-w-none rounded-lg border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 p-6 leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            // PMID links get pill-style badge rendering so it's
            // obvious they're individual clickable citations (one
            // click = open that paper on PubMed). CURIE / other links
            // keep the plain inline-link style.
            const isPmid =
              typeof href === "string" &&
              href.includes("pubmed.ncbi.nlm.nih.gov");
            if (isPmid) {
              return (
                <a
                  href={href}
                  target="_blank"
                  rel="noreferrer"
                  title="Open this paper on PubMed"
                  className="no-underline inline-flex items-center font-mono text-[0.78em] leading-none text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-950/40 hover:bg-blue-100 dark:hover:bg-blue-900/60 border border-blue-200 dark:border-blue-900 px-1.5 py-0.5 mx-0.5 rounded transition-colors"
                >
                  {children}
                </a>
              );
            }
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-blue-600 dark:text-blue-400 hover:underline font-mono text-[0.95em]"
              >
                {children}
              </a>
            );
          },
          code: ({ children }) => (
            <code className="font-mono text-[0.9em] px-1 py-0.5 rounded bg-zinc-100 dark:bg-zinc-800">
              {children}
            </code>
          ),
        }}
      >
        {enhanced}
      </ReactMarkdown>
    </article>
  );
}

// linkifyCitations + linkifyCURIEs are imported from @/lib/linkify so
// the in-app prose AND the PDF / Markdown exports share one
// implementation. see that file for the regex + outer-bracket
// rationale.
