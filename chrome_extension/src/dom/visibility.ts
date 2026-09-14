export function isVisible(element: Element): boolean {
  for (let current: Element | null = element; current !== null; current = current.parentElement) {
    if (current.hasAttribute("hidden") || current.getAttribute("aria-hidden") === "true") return false;
    const style = current.ownerDocument.defaultView?.getComputedStyle(current);
    if (style?.display === "none" || style?.visibility === "hidden" || style?.visibility === "collapse" || style?.opacity === "0") return false;
  }
  return Array.from(element.getClientRects()).some((rect) => rect.width > 0 && rect.height > 0);
}

export function visibleElements<T extends Element>(root: ParentNode, selector: string): T[] {
  return Array.from(root.querySelectorAll<T>(selector)).filter(isVisible);
}

export function visibleText(element: Element | null): string | undefined {
  if (element === null || !isVisible(element)) return undefined;
  const nodeFilter = element.ownerDocument.defaultView?.NodeFilter;
  if (nodeFilter === undefined) return undefined;
  const walker = element.ownerDocument.createTreeWalker(element, nodeFilter.SHOW_TEXT);
  const parts: string[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (node.parentElement !== null && isVisible(node.parentElement)) parts.push(node.nodeValue ?? "");
  }
  const text = parts.join("").trim();
  return text.length === 0 ? undefined : text;
}
