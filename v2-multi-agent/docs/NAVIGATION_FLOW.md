# Navigation Flow — How the SPA Routing Works

## Overview

The challenge site is a React SPA (Single Page Application) hosted on Netlify **without** SPA redirect rules. This creates critical constraints on navigation.

---

## Valid URL Patterns

| URL | Valid? | Notes |
|-----|--------|-------|
| `/` | YES | Home page, only valid entry point |
| `/stepN?version=V` | ONLY VIA SPA | Works only through React Router |
| `/finish` | ONLY VIA SPA | Completion page |
| Any other URL | NO | Returns Netlify 404 |

---

## How Navigation Works

### 1. Initial Load (Browser → Server)

```
User navigates to: https://serene-frangipane-7fd25b.netlify.app/
                                        ↓
Netlify serves: index.html + JavaScript bundle
                                        ↓
React app boots, React Router takes control
                                        ↓
User clicks START → React Router navigates to /step1?version=V
```

### 2. Step-to-Step Navigation (Client-Side Only)

```
User on /step5?version=1
            ↓
Submits correct code
            ↓
React validates code
            ↓
React Router pushes /step6?version=1 (NO SERVER REQUEST)
            ↓
React re-renders Step 6 component
```

### 3. What Happens on Refresh/Direct Navigation

```
User refreshes on /step5?version=1
            ↓
Browser requests: GET /step5?version=1
            ↓
Netlify has NO rewrite rule for /step*
            ↓
Netlify returns: 404 Page Not Found
```

---

## Why `page.reload()` Breaks Everything

```python
# BAD - Causes 404
page.reload()  # Browser requests /stepN directly → 404

# BAD - Causes 404
page.goto(f"{BASE_URL}/step{step}?version={version}")

# GOOD - Uses React Router
# Only navigate to home, let React handle routing
page.goto(BASE_URL)  # Goes to /
```

---

## Recovery from Stale Content (Multi-Tab Bug)

After multi-tab challenges, React sometimes doesn't re-render. The fix:

```python
def fix_stale_content(page, expected_step, version):
    # Step 1: Full page load to home (clean React state)
    page.goto('https://serene-frangipane-7fd25b.netlify.app')
    page.wait_for_load_state('networkidle')

    # Step 2: Use pushState + popstate to route
    target_path = f'/step{expected_step}?version={version}'
    page.evaluate(f'''() => {{
        window.history.pushState({{}}, '', '{target_path}');
        window.dispatchEvent(new PopStateEvent('popstate', {{state: {{}}}}));
    }}''')
```

This works because:
1. `goto('/')` does full page reload → fresh React app
2. `pushState` changes URL without navigation
3. `popstate` event triggers React Router's listener
4. React Router renders the correct step component

---

## Step Completion Detection

### Primary Signal: URL Change

```python
current_url = page.url
# ... submit code ...
for _ in range(5):
    page.wait_for_timeout(150)
    if page.url != current_url:
        return True  # Step completed!
```

### Secondary Signal: Body Content (Step 30 Only)

```python
if step == 30:
    body = page.inner_text('body').lower()
    if any(w in body for w in ['congratulat', 'complete', 'finished']):
        return True  # Challenge complete!
```

---

## What Triggers "Page Not Found"

Based on evidence from failure screenshots and learnings.jsonl:

### Cause 1: Clicking `<a>` Tags

```html
<!-- Somewhere on the page -->
<a href="/follow">FOLLOW</a>   <!-- Clicking this → 404 -->
<a href="#section4">Section 4</a>  <!-- May work OR may 404 -->
```

When an `<a>` tag is clicked:
1. Browser follows the `href`
2. If `href` is relative path (not hash): full navigation
3. Netlify receives request for `/follow`
4. No rewrite rule → 404

### Cause 2: Form with Wrong Action

```html
<form action="/submit">  <!-- Wrong! -->
    <button type="submit">Go</button>
</form>
```

### Cause 3: JavaScript Navigation

```javascript
// Decoy code might do this
window.location.href = '/trap';  // → 404
```

---

## Navigation Safety Checklist

Before ANY click:
- [ ] Check `element.tagName !== 'A'`
- [ ] Check `element.href` is undefined or empty
- [ ] Check element is within challenge area (y < 500px)
- [ ] Save current URL for comparison

After ANY click:
- [ ] Wait 100-300ms
- [ ] Check if URL changed unexpectedly
- [ ] If on 404 page, recover immediately

Recovery procedure:
1. Detect 404 (check for "Page not found" text)
2. Navigate to home: `page.goto(BASE_URL)`
3. Use pushState to restore previous step
4. Retry the step

---

## URL State Machine

```
                    ┌──────────────┐
                    │   HOME (/)   │
                    └──────┬───────┘
                           │ Click START
                           ▼
          ┌────────────────────────────────────┐
          │         /step1?version=V           │
          └────────────────┬───────────────────┘
                           │ Submit correct code
                           ▼
          ┌────────────────────────────────────┐
          │         /step2?version=V           │
          └────────────────┬───────────────────┘
                           │ ... repeat ...
                           ▼
          ┌────────────────────────────────────┐
          │        /step30?version=V           │
          └────────────────┬───────────────────┘
                           │ Submit correct code
                           ▼
          ┌────────────────────────────────────┐
          │           /finish                  │
          └────────────────────────────────────┘

    ─── DANGER PATHS (lead to 404) ───

    Any /stepN directly → 404
    Any /sectionN → 404
    Any /follow, /continue, /next → 404
    page.reload() on /stepN → 404
```

---

## Key Takeaways

1. **Only `/` is a valid entry point** — all other navigation must be through React Router
2. **Never use `page.reload()`** — it will 404
3. **Never click `<a>` tags** — they cause server-side navigation
4. **URL change = success signal** — monitor URL after every action
5. **Recovery is possible** — go to `/` then pushState to restore
