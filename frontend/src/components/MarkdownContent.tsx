import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

interface MarkdownContentProps {
  text: string;
  className?: string;
  testId?: string;
  allowedElements?: string[];
}

export function MarkdownContent({
  text,
  className = "message-content message-markdown",
  testId = "message-markdown",
  allowedElements,
}: MarkdownContentProps) {
  return (
    <div className={className} data-testid={testId}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeSanitize]}
        allowedElements={allowedElements}
        unwrapDisallowed={Boolean(allowedElements)}
        components={{
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
