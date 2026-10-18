"use strict";

// One renderer for untrusted board prose. The pinned parser escapes raw HTML;
// no plugins may inject markup or derive element IDs/names from user text.
const Markdown = (() => {
  const parser = markdownit({html:false, breaks:true, linkify:true, typographer:false, maxNesting:20});
  const validate = parser.validateLink;
  parser.validateLink = value => {
    if (!validate(value)) return false;
    try { return ["http:", "https:", "mailto:"].includes(new URL(value, location.href).protocol); }
    catch { return false; }
  };
  const linkOpen = parser.renderer.rules.link_open || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
  parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet("rel", "noopener noreferrer");
    return linkOpen(tokens, index, options, env, renderer);
  };
  const image = parser.renderer.rules.image;
  parser.renderer.rules.image = (tokens, index, options, env, renderer) => {
    const token = tokens[index], src = token.attrGet("src");
    const alt = renderer.renderInlineAsText(token.children, options, env);
    // Keep the existing same-origin media policy. Remote images remain links,
    // so reading a message never loads a third-party tracking image.
    const url = new URL(src, location.href);
    if (url.origin !== location.origin) return `<a href="${parser.utils.escapeHtml(url.href)}" rel="noopener noreferrer">${parser.utils.escapeHtml(alt || "Image")}</a>`;
    token.attrSet("loading", "lazy"); token.attrSet("referrerpolicy", "no-referrer");
    return image(tokens, index, options, env, renderer);
  };
  function highlightMentions(root, targets) {
    const names = (targets || []).map(item => `@${item.name}`).sort((a,b) => b.length-a.length);
    if (!names.length) return;
    const escaped = names.map(name => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const pattern = new RegExp(`(${escaped.join("|")})`, "gi");
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) if (!walker.currentNode.parentElement.closest("a,code,pre")) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const parts = node.data.split(pattern);
      if (parts.length < 2) continue;
      const fragment = document.createDocumentFragment();
      for (const part of parts) {
        if (names.some(name => name.toLowerCase() === part.toLowerCase())) {
          const mark = document.createElement("span"); mark.className="mention"; mark.textContent=part; fragment.append(mark);
        } else fragment.append(document.createTextNode(part));
      }
      node.replaceWith(fragment);
    }
  }
  function codeContainer(pre, label = "Copy code") {
    const wrapper = document.createElement("div"), copy = document.createElement("button");
    wrapper.className = "code-block";
    copy.type = "button"; copy.className = "code-copy"; copy.textContent = label;
    copy.setAttribute("aria-live", "polite");
    let reset;
    copy.addEventListener("click", async () => {
      clearTimeout(reset);
      try {
        await navigator.clipboard.writeText(pre.textContent);
        copy.textContent = "Copied!";
      } catch {
        const selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(pre); selection.removeAllRanges(); selection.addRange(range);
        copy.textContent = "Select and copy";
      }
      reset = setTimeout(() => { copy.textContent = label; }, 2000);
    });
    pre.replaceWith(wrapper);
    wrapper.append(copy, pre);
    return wrapper;
  }
  function codeBlock(value, className = "", label = "Copy code") {
    const pre = document.createElement("pre");
    pre.className = className; pre.textContent = String(value || "");
    return codeContainer(pre, label);
  }
  function block(value, className = "", mentions = []) {
    const root = document.createElement("div");
    root.className = `markdown-body ${className}`.trim();
    root.innerHTML = parser.render(String(value || ""));
    highlightMentions(root, mentions);
    root.querySelectorAll("pre").forEach(pre => codeContainer(pre));
    return root;
  }
  return {block, codeBlock};
})();
