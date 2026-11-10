import { useEffect, useState, type AnchorHTMLAttributes, type MouseEvent } from "react";

// Four routes do not need a routing library.
export function usePath(): string {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  return path;
}

export function navigate(to: string): void {
  if (to === window.location.pathname) return;
  window.history.pushState({}, "", to);
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.scrollTo(0, 0);
}

export function Link({ href, onClick, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.shiftKey || rest.target === "_blank") return;
    e.preventDefault();
    navigate(href);
  };
  return <a href={href} onClick={handle} {...rest} />;
}
