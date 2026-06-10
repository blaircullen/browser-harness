"""Agent-editable browser helpers.

Add task-specific browser primitives here. Core helpers from browser_harness.helpers
load this file when BH_AGENT_WORKSPACE points at this directory, or when this
repo's default agent-workspace exists.
"""

import contextlib
import json

# Core primitives. This module is exec'd standalone (its functions keep this
# file as their __globals__), so unlike helpers defined inside the core module
# we must import cdp/js explicitly or calls below raise NameError.
from browser_harness.helpers import (
    cdp, close_tab, current_tab, js, new_tab, switch_tab,
)


def _result_dict(res):
    """Narrow a js() return to a dict.

    js() can deserialize any JS value (number, string, dict, ...), so its return
    type is a union. The form helpers below always evaluate to an object, but the
    type checker can't prove that — and a stray non-object (e.g. a page that
    threw before returning) shouldn't crash a `.get()`. Return the dict as-is, or
    {} for anything else.
    """
    return res if isinstance(res, dict) else {}


# --- form-heavy SPA admin helpers (Google Ads, Shopify admin, etc.) ----------
#
# These cut round-trips when driving labeled forms in Angular/React admin UIs.
# Coordinate clicks still work, but for inputs/radios identified by their
# visible label text these are more reliable than pixel hunting:
#   set_desktop_viewport()  -> bring below-the-fold controls into reach
#   fill_by_label()         -> set an input found by label/aria/placeholder
#   select_option_by_label() -> pick a radio/option AND verify it stuck
#   click_text()            -> click a button/link by its visible text
#   dump_links()            -> cheap nav discovery without screenshots
#   field_value()           -> read back what a labeled field holds


# Shared JS: resolve a form control (input/textarea/select) from a human label.
# Match order, all case-insensitive substring: aria-label -> placeholder ->
# associated <label> (for=id, ancestor <label>, or aria-labelledby) -> name/id.
# Returns the element or null. Skips hidden/disabled controls so a stray
# off-screen field doesn't shadow the visible one.
_FIND_CONTROL_JS = r"""
function __bhFindControl(label){
  const want = label.toLowerCase().trim();
  const ctrls = Array.from(document.querySelectorAll('input,textarea,select'))
    .filter(e=>e.type!=='hidden' && !e.disabled && e.offsetParent!==null);
  const labelText = (e)=>{
    let t = '';
    if(e.id){
      const l = document.querySelector('label[for='+CSS.escape(e.id)+']');
      if(l) t += ' '+l.textContent;
    }
    const anc = e.closest('label');
    if(anc) t += ' '+anc.textContent;
    const ll = e.getAttribute('aria-labelledby');
    if(ll) ll.split(/\s+/).forEach(id=>{const n=document.getElementById(id); if(n) t+=' '+n.textContent;});
    return t;
  };
  const score = (e)=>{
    const al = (e.getAttribute('aria-label')||'').toLowerCase();
    const ph = (e.placeholder||'').toLowerCase();
    const lt = labelText(e).toLowerCase();
    const nm = ((e.name||'')+' '+(e.id||'')).toLowerCase();
    if(al.includes(want)) return 4;
    if(ph.includes(want)) return 3;
    if(lt.includes(want)) return 2;
    if(nm.includes(want)) return 1;
    return 0;
  };
  let best=null, bestScore=0;
  for(const e of ctrls){const s=score(e); if(s>bestScore){bestScore=s; best=e;}}
  return bestScore>0 ? best : null;
}
"""


def set_desktop_viewport(w=1440, h=1100):
    """Enlarge the emulated viewport so below-the-fold controls are reachable.

    The default CDP viewport is tiny (~756x469); coordinate clicks on buttons/
    radios further down the page miss or mis-target. Call this once before any
    coordinate work on a form. Returns the new {w, h} as confirmed by the page.
    """
    cdp("Emulation.setDeviceMetricsOverride", width=w, height=h,
        deviceScaleFactor=1, mobile=False)
    return {"w": js("innerWidth"), "h": js("innerHeight")}


def fill_by_label(label, value):
    """Fill the input/textarea matching `label` (case-insensitive substring of
    aria-label / placeholder / associated <label> / name).

    Sets the value via the native setter and dispatches input+change so
    framework-controlled fields (Angular/React/Vue) actually register it.
    Returns {ok, label, value} with the value read back from the element, or
    {ok: False, error} if no matching control was found.
    """
    res = js(_FIND_CONTROL_JS + r"""
return (function(){
  const e = __bhFindControl(""" + json.dumps(label) + r""");
  if(!e) return {ok:false, error:'no control matching label'};
  e.focus();
  const proto = e.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype
              : e.tagName==='SELECT'   ? window.HTMLSelectElement.prototype
              : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto,'value').set;
  setter.call(e, """ + json.dumps(str(value)) + r""");
  e.dispatchEvent(new Event('input',{bubbles:true}));
  e.dispatchEvent(new Event('change',{bubbles:true}));
  return {ok:true, value:e.value, name:(e.name||e.id||'')};
})();
""")
    res = _result_dict(res)
    if res.get("ok"):
        return {"ok": True, "label": label, "value": res["value"], "name": res.get("name", "")}
    return {"ok": False, "error": res.get("error", "fill failed"), "label": label}


def field_value(label):
    """Read back the current value of the input/textarea matching `label`.
    Returns the value string, or None if no control matched."""
    return js(_FIND_CONTROL_JS + r"""
return (function(){
  const e = __bhFindControl(""" + json.dumps(label) + r""");
  return e ? e.value : null;
})();
""")


def click_text(text, role=None):
    """Click the element whose trimmed visible text equals (preferred) or
    contains `text`. Optionally restrict by tag/role (e.g. 'button', 'a').

    Scrolls the match into view first, then dispatches a real click. Prefers an
    exact text match over a substring, and the smallest matching element so an
    outer container isn't clicked instead of the actual control. Returns
    {ok, tag, text} describing what was clicked, or {ok: False, error}.
    """
    role_js = json.dumps(role) if role else "null"
    res = js(r"""
return (function(){
  const want = """ + json.dumps(text) + r""".toLowerCase().trim();
  const role = """ + role_js + r""";
  const clickable = 'a,button,[role=button],[role=link],input[type=submit],input[type=button],[onclick],summary';
  let nodes = Array.from(document.querySelectorAll(clickable));
  // Fall back to a broader sweep if no semantic control carries the text.
  if(!nodes.some(n=>(n.textContent||n.value||'').toLowerCase().includes(want)))
    nodes = Array.from(document.querySelectorAll('*'));
  const vis = (e)=>e.offsetParent!==null || e.getClientRects().length>0;
  const matchRole = (e)=>{
    if(!role) return true;
    const r=role.toLowerCase();
    return e.tagName.toLowerCase()===r || (e.getAttribute('role')||'').toLowerCase()===r
        || (r==='button' && (e.tagName==='BUTTON'||e.type==='submit'||e.type==='button'));
  };
  const txt=(e)=>(e.tagName==='INPUT'?(e.value||''):e.textContent||'').toLowerCase().trim();
  const cands = nodes.filter(e=>vis(e)&&matchRole(e)&&txt(e).includes(want));
  if(!cands.length) return {ok:false, error:'no visible element with text'};
  const exact = cands.filter(e=>txt(e)===want);
  const pool = exact.length?exact:cands;
  // smallest text wins -> the leaf control, not its wrapper
  pool.sort((a,b)=>txt(a).length-txt(b).length);
  const el = pool[0];
  el.scrollIntoView({block:'center'});
  el.click();
  return {ok:true, tag:el.tagName, text:(el.textContent||el.value||'').trim().slice(0,80)};
})();
""")
    res = _result_dict(res)
    if res.get("ok"):
        return {"ok": True, "tag": res["tag"], "text": res["text"]}
    return {"ok": False, "error": res.get("error", "click failed"), "want": text}


def select_option_by_label(option_label):
    """Select the radio/option whose visible option label matches `option_label`,
    then VERIFY it became checked. Safety-critical: confirms the *right* control
    is selected (not just that a click fired) before returning success.

    Matches a radio/checkbox by its associated <label>, aria-label, or the text
    of a [role=radio]/[role=option] element. Requires an exact (case-insensitive,
    trimmed) label match unless exactly one control contains the substring — this
    refuses ambiguous picks (e.g. 'Read' when both 'Read only' and 'Read/write'
    exist) rather than guessing. Returns {ok, label, checked} on success, or
    {ok: False, error} (with `candidates` listed when the match was ambiguous).
    """
    res = js(r"""
return (function(){
  const want = """ + json.dumps(option_label) + r""".toLowerCase().trim();
  const labelFor = (e)=>{
    let t='';
    if(e.id){const l=document.querySelector('label[for='+CSS.escape(e.id)+']'); if(l)t+=' '+l.textContent;}
    const anc=e.closest('label'); if(anc)t+=' '+anc.textContent;
    if(e.getAttribute('aria-label'))t+=' '+e.getAttribute('aria-label');
    return t.toLowerCase().replace(/\s+/g,' ').trim();
  };
  // Native radios/checkboxes plus ARIA option/radio widgets.
  const native = Array.from(document.querySelectorAll('input[type=radio],input[type=checkbox]'))
    .filter(e=>e.offsetParent!==null && !e.disabled);
  const aria = Array.from(document.querySelectorAll('[role=radio],[role=option],[role=menuitemradio]'))
    .filter(e=>e.offsetParent!==null);
  const items = native.map(e=>({el:e, label:labelFor(e)||(e.value||'').toLowerCase(), native:true}))
    .concat(aria.map(e=>({el:e, label:(e.textContent||e.getAttribute('aria-label')||'').toLowerCase().replace(/\s+/g,' ').trim(), native:false})));
  const exact = items.filter(i=>i.label===want);
  let pick;
  if(exact.length===1){pick=exact[0];}
  else if(exact.length>1){return {ok:false, error:'ambiguous (multiple exact)', candidates:exact.map(i=>i.label)};}
  else {
    const sub = items.filter(i=>i.label.includes(want));
    if(sub.length===1){pick=sub[0];}
    else if(sub.length===0){return {ok:false, error:'no option matching label', candidates:items.map(i=>i.label).filter(Boolean)};}
    else {return {ok:false, error:'ambiguous (multiple substring matches) - refusing to guess', candidates:sub.map(i=>i.label)};}
  }
  const el = pick.el;
  el.scrollIntoView({block:'center'});
  el.click();
  // Verify it actually stuck.
  let checked;
  if(pick.native){ checked = !!el.checked; }
  else { const ac=el.getAttribute('aria-checked')||el.getAttribute('aria-selected'); checked = ac==='true'; }
  return {ok:checked, label:pick.label, checked:checked,
          error: checked?undefined:'clicked but control did not become checked'};
})();
""")
    res = _result_dict(res)
    if res.get("ok"):
        return {"ok": True, "label": res["label"], "checked": True}
    out = {"ok": False, "error": res.get("error", "select failed"), "want": option_label}
    if res.get("candidates"):
        out["candidates"] = res["candidates"]
    return out


def dump_links(pattern=None):
    """Return [{text, href}] for visible anchors. If `pattern` (regex) is given,
    keep only links whose text or href matches it (case-insensitive). Cheap nav
    discovery — avoids a screenshot when you just need to find a link to click.
    """
    pat_js = json.dumps(pattern) if pattern else "null"
    return js(r"""
return (function(){
  const pat = """ + pat_js + r""";
  const re = pat ? new RegExp(pat, 'i') : null;
  return Array.from(document.querySelectorAll('a[href]'))
    .filter(a=>a.offsetParent!==null)
    .map(a=>({text:(a.textContent||'').replace(/\s+/g,' ').trim(), href:a.href}))
    .filter(a=>a.text || a.href)
    .filter(a=>!re || re.test(a.text) || re.test(a.href));
})();
""")


# --- tab maintenance ---------------------------------------------------------
#
# Harness sessions accumulate throwaway tabs (about:blank, chrome://newtab,
# dupes from repeated new_tab) which bloat browser memory. These close the junk
# safely and give scratch work a self-cleaning tab so it never leaks.
#   close_stale_tabs() -> sweep junk + dupes, never touching protected/live tabs
#   temp_tab()         -> context manager: scratch tab that always closes
#   open_scratch_tab() / close_scratch_tab() -> non-CM equivalent


# Junk URLs that are always safe to close, and fake/internal targets that
# list_tabs() with include_chrome=False already drops. We re-check the prefix
# here because close_stale_tabs reads the full target list itself.
_JUNK_URLS = {"about:blank", "chrome://newtab/", "chrome://newtab", ""}


def _norm_url(url):
    """Normalize for dedupe: drop a trailing slash and an obvious cache-buster
    query (?_=..., ?t=..., ?v=..., ?cb=...). Conservative — only strips a
    single whole query when it's just one of those volatile keys, so real
    distinct URLs (?id=, ?page=) are never collapsed together."""
    u = url.split("#", 1)[0]
    if "?" in u:
        base, q = u.split("?", 1)
        if "&" not in q and "=" in q:
            key = q.split("=", 1)[0].lower()
            if key in ("_", "t", "v", "cb", "ts", "cachebust", "cache"):
                u = base
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    return u


def close_stale_tabs(keep=None, close_blank=True, dedupe=True, keep_active=True):
    """Close junk/duplicate tabs without touching real, in-use ones.

    Always closes omnibox-popup / internal fake targets. With close_blank
    (default), also closes about:blank and chrome://newtab. With dedupe
    (default), collapses tabs sharing a normalized URL down to one.

    keep:        list of URL substrings that must NEVER be closed (protected set).
    keep_active: never close the currently-attached/active tab (default True).

    Conservative by design — anything not clearly junk or a duplicate is kept.
    Returns {"closed": [urls], "kept": [urls]}.
    """
    keep = keep or []
    targets = [t for t in cdp("Target.getTargets")["targetInfos"]
               if t.get("type") == "page"]

    try:
        active_id = current_tab()["targetId"] if keep_active else None
    except Exception:
        active_id = None

    def is_protected(url):
        return any(s in url for s in keep)

    def is_fake(url):
        # Omnibox popups and other chrome-internal surfaces are fake page
        # targets (SKILL.md). The user's real about:blank / newtab are handled
        # separately via close_blank.
        return url.startswith("chrome://omnibox-popup") or "omnibox-popup" in url

    def is_blank(url):
        return url in _JUNK_URLS or url.startswith("chrome://newtab")

    closed, kept = [], []
    seen = {}  # normalized URL -> already kept a copy

    for t in targets:
        tid = t["targetId"]
        url = t.get("url", "")

        if tid == active_id:
            kept.append(url); continue
        if is_protected(url):
            kept.append(url); continue

        # Always reap fake/internal omnibox targets.
        if is_fake(url):
            try: close_tab(tid); closed.append(url)
            except Exception: kept.append(url)
            continue

        if close_blank and is_blank(url):
            try: close_tab(tid); closed.append(url)
            except Exception: kept.append(url)
            continue

        if dedupe:
            norm = _norm_url(url)
            if norm in seen:
                try: close_tab(tid); closed.append(url)
                except Exception: kept.append(url)
                continue
            seen[norm] = True

        kept.append(url)

    return {"closed": closed, "kept": kept}


@contextlib.contextmanager
def temp_tab(url="about:blank"):
    """Context manager for a throwaway tab. Opens `url`, yields its targetId, and
    GUARANTEES the tab is closed on exit — even if the body raises — then restores
    the previously-active tab. Use this for any scratch/testing navigation so it
    never leaks a tab.

        with temp_tab("https://example.com") as tid:
            ...                      # work in the scratch tab
        # tab is gone, prior tab re-selected
    """
    try:
        prev = current_tab()["targetId"]
    except Exception:
        prev = None
    tid = new_tab(url)
    try:
        yield tid
    finally:
        try:
            close_tab(tid)
        finally:
            if prev is not None:
                try: switch_tab(prev)
                except Exception: pass


# Non-context-manager equivalent for code that can't use `with` (e.g. opening a
# scratch tab in one call and closing it in a later one). Tracks a single
# outstanding scratch target id.
_scratch = {"tid": None, "prev": None}


def open_scratch_tab(url="about:blank"):
    """Open a tracked scratch tab; remembers the prior tab. Pair with
    close_scratch_tab(). Returns the new targetId."""
    try:
        _scratch["prev"] = current_tab()["targetId"]
    except Exception:
        _scratch["prev"] = None
    _scratch["tid"] = new_tab(url)
    return _scratch["tid"]


def close_scratch_tab():
    """Close the tab opened by open_scratch_tab() and restore the prior tab.
    No-op if none is tracked. Returns the closed targetId or None."""
    tid = _scratch["tid"]
    if tid is None:
        return None
    try:
        close_tab(tid)
    finally:
        prev = _scratch["prev"]
        _scratch["tid"] = None
        _scratch["prev"] = None
        if prev is not None:
            try: switch_tab(prev)
            except Exception: pass
    return tid
