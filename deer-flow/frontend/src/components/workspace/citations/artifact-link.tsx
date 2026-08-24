import type { AnchorHTMLAttributes } from "react";

import { cn } from "@/lib/utils";

import { CitationLink } from "./citation-link";

function isExternalUrl(href: string | undefined): boolean {
  return !!href && /^https?:\/\//.test(href);
}

/** Link renderer for artifact markdown: citation: prefix → CitationLink, otherwise underlined text. */
export function ArtifactLink(props: AnchorHTMLAttributes<HTMLAnchorElement>) {
  if (typeof props.children === "string") {
    const match = /^citation:(.+)$/.exec(props.children);
    if (match) {
      const [, text] = match;
      return <CitationLink {...props}>{text}</CitationLink>;
    }
  }
  const { className, target, rel, onClick, ...rest } = props;
  const external = isExternalUrl(props.href);
  const handleClick: AnchorHTMLAttributes<HTMLAnchorElement>["onClick"] = (
    event,
  ) => {
    onClick?.(event);
    if (!external || event.defaultPrevented) return;
    event.preventDefault();
    try {
      if (navigator.clipboard && props.href) {
        void navigator.clipboard.writeText(props.href);
      }
    } catch {}
    console.info("Artifact preview link navigation blocked:", props.href);
  };
  return (
    <a
      {...rest}
      className={cn(
        "text-primary underline decoration-primary/30 underline-offset-2 hover:decoration-primary/60 transition-colors",
        className,
      )}
      onClick={handleClick}
      target={target ?? (external ? "_blank" : undefined)}
      rel={rel ?? (external ? "noopener noreferrer" : undefined)}
    />
  );
}
