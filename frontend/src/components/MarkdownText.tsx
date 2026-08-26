import ReactMarkdown from 'react-markdown'

/**
 * Renders Groq-generated Markdown (bold, bullet lists, blockquotes) as
 * real styled HTML instead of raw text -- without this, a generated
 * "**En resume**" would show literal asterisks on screen. No raw HTML
 * support (react-markdown's default: only Markdown syntax, safe by
 * construction against injected tags) since every caller's content is
 * LLM-generated text, never user-authored HTML.
 *
 * Usage:
 *   <MarkdownText>{narrative.texte}</MarkdownText>
 */
export function MarkdownText({ children }: { children: string }) {
  return (
    <div className="text-sm text-gray-800">
      <ReactMarkdown
        components={{
          p: ({ ...props }) => <p className="mb-3 last:mb-0" {...props} />,
          strong: ({ ...props }) => <strong className="font-semibold text-gray-900" {...props} />,
          ul: ({ ...props }) => <ul className="mb-3 list-disc space-y-1 pl-5 last:mb-0" {...props} />,
          ol: ({ ...props }) => <ol className="mb-3 list-decimal space-y-1 pl-5 last:mb-0" {...props} />,
          li: ({ ...props }) => <li {...props} />,
          blockquote: ({ ...props }) => (
            <blockquote
              className="mb-3 border-l-4 border-indigo-200 pl-3 italic text-gray-600 last:mb-0"
              {...props}
            />
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
