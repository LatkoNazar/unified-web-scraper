"""Pure trusted browser actions: observed bindings, fixed JavaScript and result checks."""

import json
from typing import Any

from bs4 import BeautifulSoup

from unified_web_parser.core.models import Page
from unified_web_parser.crawling.errors import FetchError
from unified_web_parser.navigation.models import NavigationAction

SERIALIZE_CONTROLS = """
const sensitive = /password|passwd|token|secret|credential|csrf|authorization|api.?key/i;
for (const el of document.querySelectorAll('input, select')) {
    const identity = [el.name, el.id, el.getAttribute('aria-label') || ''].join(' ');
    if (sensitive.test(identity)) continue;
    if (el.tagName === 'SELECT') {
        for (const option of el.options) option.toggleAttribute('selected', option.selected);
    } else if (['search', 'text'].includes(el.type) &&
               (el.type === 'search' || el.getAttribute('role') === 'searchbox' ||
                /^(q|query|search|search_term|text|s)$/i.test(el.name))) {
        el.setAttribute('value', el.value);
    }
}
"""


def snapshot_binding(page: Page, selector: str | None) -> dict[str, str] | None:
    if not selector:
        return None
    try:
        element = BeautifulSoup(page.html, "html.parser").select_one(selector)
    except Exception as exc:
        raise FetchError(str(page.url), "Invalid observed navigation selector") from exc
    if element is None:
        raise FetchError(str(page.url), "Navigation element is absent from its source snapshot")
    binding = {"tag": element.name}
    for attribute in ("id", "name", "type", "href", "action", "method", "formaction"):
        value = element.get(attribute)
        if isinstance(value, str):
            binding[attribute] = value
    if element.name not in {"input", "textarea", "select", "form"}:
        binding["text"] = " ".join(element.get_text(" ", strip=True).split())[:180]
    return binding


def check_execution(page: Page, result: Any) -> dict[str, Any] | None:
    execution = getattr(result, "js_execution_result", None)
    if not isinstance(execution, dict) or execution.get("success") is not True:
        raise FetchError(str(page.url), "Browser action JavaScript did not execute successfully")
    outcome = None
    for item in execution.get("results", []):
        if not isinstance(item, dict):
            continue
        if item.get("success") is False or item.get("ok") is False:
            raise FetchError(
                str(page.url), f"Browser action rejected: {item.get('error', 'failed')}"
            )
        if "ok" in item:
            outcome = item
    return outcome


def build_action_script(
    page: Page, action: NavigationAction, query: str | None, *, timeout: float, render_delay: float
) -> str:
    parameters = {
        "url": str(page.url),
        "kind": action.kind,
        "selector": action.selector,
        "input_selector": action.input_selector,
        "submit_selector": action.submit_selector,
        "option_value": action.option_value,
        "query": query,
        "filter_selectors": action.filter_selectors,
        "filter_query_names": action.filter_query_names,
        "bindings": {
            selector: snapshot_binding(page, selector)
            for selector in (
                action.selector,
                action.input_selector,
                action.submit_selector,
                *action.filter_selectors,
            )
            if selector
        },
        "timeout_ms": min(int(timeout * 1000), 8000),
        "settle_ms": max(100, int(render_delay * 1000)),
    }
    payload = json.dumps(parameters, ensure_ascii=False, allow_nan=False)
    # Only this fixed implementation is executable. Observed bindings/query are JSON data.
    return (
        "const p = "
        + payload
        + ";\n"
        + r"""
if (location.href !== p.url) return {ok:false, error:'stale browser URL'};
const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
for (const [selector, binding] of Object.entries(p.bindings)) {
    const element = document.querySelector(selector);
    if (!element || element.tagName.toLowerCase() !== binding.tag)
        return {ok:false, error:'stale browser element'};
    for (const [name, value] of Object.entries(binding)) {
        if (name === 'tag') continue;
        const current = name === 'text' ? normalize(element.textContent).slice(0,180)
                                       : element.getAttribute(name);
        if (current !== value) return {ok:false, error:'browser element binding changed'};
    }
    for (const name of ['href', 'action', 'formaction']) {
        const value = element.getAttribute(name);
        if (!value) continue;
        const target = new URL(value, location.href);
        if (!['http:', 'https:'].includes(target.protocol) ||
            target.hostname !== location.hostname ||
            target.port !== location.port || target.username || target.password)
            return {ok:false, error:'external action target'};
    }
}
const before = document.documentElement.outerHTML;
const beforeY = window.scrollY;
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
if (p.kind === 'search') {
    const input = document.querySelector(p.input_selector);
    if (!input || !['INPUT','TEXTAREA'].includes(input.tagName) ||
        !['search','text','textarea'].includes(input.type) || typeof p.query !== 'string')
        return {ok:false, error:'search input is unavailable'};
    const identity = [input.name,input.id,input.getAttribute('aria-label') || ''].join(' ');
    if (/password|passwd|token|secret|credential|csrf|authorization|api.?key/i.test(identity))
        return {ok:false, error:'sensitive input is not navigable'};
    for (const selector of p.filter_selectors) {
        const filter = document.querySelector(selector);
        if (!filter || filter === input) continue;
        // Only inventoried optional controls are reset; consent/security fields are excluded.
        if (filter.tagName === 'SELECT') {
            filter.selectedIndex = -1;
            for (const option of filter.options) option.removeAttribute('selected');
        } else if (filter.type === 'checkbox' || filter.type === 'radio') {
            filter.checked = false;
            filter.removeAttribute('checked');
        } else {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
            setter.set.call(filter,'');
            filter.setAttribute('value','');
        }
        filter.disabled = true;
        filter.setAttribute('disabled','');
        filter.dispatchEvent(new Event('input',{bubbles:true}));
        filter.dispatchEvent(new Event('change',{bubbles:true}));
    }
    input.focus();
    const prototype = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                                  : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype,'value').set.call(input,p.query);
    input.setAttribute('value',p.query);
    input.dispatchEvent(new Event('input',{bubbles:true}));
    input.dispatchEvent(new Event('change',{bubbles:true}));
    await pause(30);
    const submit = p.submit_selector && document.querySelector(p.submit_selector);
    const form = input.form;
    if (form) {
        const override = submit && submit.hasAttribute('formaction');
        const target = new URL((override && submit.formAction) || form.action || location.href);
        if (target.hostname !== location.hostname || target.port !== location.port ||
            !['http:', 'https:'].includes(target.protocol) || target.username || target.password)
            return {ok:false, error:'external search form target'};
        for (const name of p.filter_query_names) target.searchParams.delete(name);
        if (override) submit.formAction = target.href;
        else form.action = target.href;
        form.target = '_self';
        if (submit) submit.click();
        else form.requestSubmit();
    } else if (submit) submit.click();
    else {
        for (const type of ['keydown','keyup']) input.dispatchEvent(
            new KeyboardEvent(type,{key:'Enter',code:'Enter',
                                   bubbles:true,cancelable:true}));
    }
} else if (p.kind === 'click') {
    const element = document.querySelector(p.selector);
    if (!element || element.disabled) return {ok:false,error:'button is unavailable'};
    element.click();
} else if (p.kind === 'select') {
    const element = document.querySelector(p.selector);
    if (!element || element.tagName !== 'SELECT' || element.disabled ||
        !Array.from(element.options).some(option =>
            option.value === p.option_value && !option.disabled))
        return {ok:false,error:'observed option is unavailable'};
    element.value = p.option_value;
    for (const option of element.options) option.toggleAttribute('selected',option.selected);
    element.dispatchEvent(new Event('input',{bubbles:true}));
    element.dispatchEvent(new Event('change',{bubbles:true}));
} else if (p.kind === 'scroll') {
    window.scrollBy(0,Math.max(200,Math.min(window.innerHeight,1000)));
} else return {ok:false,error:'unsupported browser action'};
await pause(p.settle_ms);
const deadline = Date.now() + p.timeout_ms;
while (Date.now() < deadline && location.href === p.url &&
       document.documentElement.outerHTML === before && window.scrollY === beforeY) await pause(50);
"""
        + SERIALIZE_CONTROLS
        + r"""
return {ok:true,changed:location.href !== p.url || document.documentElement.outerHTML !== before ||
                        window.scrollY !== beforeY,
        url:location.href,scroll_y:window.scrollY,
        scroll_height:Math.max(document.documentElement.scrollHeight,
                               document.body ? document.body.scrollHeight : 0),
        viewport_height:window.innerHeight};
"""
    )
