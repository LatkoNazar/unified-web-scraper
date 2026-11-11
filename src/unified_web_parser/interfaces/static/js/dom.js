// Small DOM primitives; importing this module does not touch the document.

export const $ = (id) => document.getElementById(id);

export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

export function setConnection(text, kind = "") {
  const node = $("connection");
  node.className = `connection ${kind}`;
  node.replaceChildren(element("span"), document.createTextNode(text));
}

export function showNotice(message) {
  $("notice").textContent = message || "";
  $("notice").hidden = !message;
}
