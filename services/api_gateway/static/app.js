/* ===========================================================================
 * Report Generator Agent — front-end behaviour
 * ---------------------------------------------------------------------------
 * Owner: ENG-2.  Single file, single IIFE, vanilla ES5-flavoured JS.
 * No modules, no bundler, no imports, no off-origin requests of any kind.
 * Loaded from base.html as: <script src="/static/app.js" defer></script>
 *
 * EVERYTHING HERE IS A PROGRESSIVE ENHANCEMENT.
 * Every page must remain fully usable with JavaScript disabled:
 *   - citation markers are real <a href="#ref-N"> links into a server-rendered
 *     appendix, so they work without us;
 *   - the progress page carries <noscript><meta http-equiv="refresh"></noscript>;
 *   - the run history table is server-rendered before we filter it.
 * Consequently every module below bails out silently when its hooks are absent
 * (the same script loads on the gallery, the form page and the run page), and
 * no module is allowed to throw in a way that stops the others booting.
 *
 * Structural classes are server-rendered and we never add or remove them.
 * We only ever write `is-` prefixed state classes, the two `data-` display
 * switches on #rg-app, and the specific text/attribute patches listed in the
 * implementation contract §6.1.
 * ======================================================================== */

(function () {
  'use strict';

  /* =======================================================================
   * 0. Tiny DOM helpers
   * ===================================================================== */

  function $(sel, root) {
    try {
      return (root || document).querySelector(sel);
    } catch (e) {
      return null;
    }
  }

  function $$(sel, root) {
    try {
      return Array.prototype.slice.call((root || document).querySelectorAll(sel));
    } catch (e) {
      return [];
    }
  }

  function on(el, type, fn, opts) {
    if (el && el.addEventListener) {
      el.addEventListener(type, fn, opts === undefined ? false : opts);
    }
  }

  function setText(el, text) {
    if (el && el.textContent !== text) {
      el.textContent = text;
    }
  }

  function closestOf(el, sel) {
    if (!el) {
      return null;
    }
    if (el.closest) {
      try {
        return el.closest(sel);
      } catch (e) {
        return null;
      }
    }
    var node = el;
    while (node && node.nodeType === 1) {
      if (node.matches && node.matches(sel)) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  /* A node is "visible" if it participates in layout. This is how we honour
   * the `data-filter="flagged"` CSS switch without knowing anything about the
   * stylesheet: hidden things simply drop out of the keyboard cycles. */
  function isVisible(el) {
    if (!el) {
      return false;
    }
    if (el.hidden) {
      return false;
    }
    if (el.getClientRects && el.getClientRects().length > 0) {
      return true;
    }
    return el.offsetParent !== null;
  }

  var NATIVE_FOCUSABLE = {a: 1, button: 1, input: 1, select: 1, textarea: 1, summary: 1, area: 1};

  function isNativelyFocusable(el) {
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'a' || tag === 'area') {
      return el.hasAttribute('href');
    }
    return NATIVE_FOCUSABLE[tag] === 1;
  }

  var FOCUSABLE_SEL = [
    'a[href]',
    'area[href]',
    'button:not([disabled])',
    'input:not([disabled]):not([type="hidden"])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    'summary',
    'iframe',
    '[tabindex]:not([tabindex="-1"])',
    '[contenteditable="true"]'
  ].join(', ');

  function focusablesIn(root) {
    return $$(FOCUSABLE_SEL, root).filter(isVisible);
  }

  /* =======================================================================
   * 1. Motion preference
   * ===================================================================== */

  var motionQuery = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  function reducedMotion() {
    return !!(motionQuery && motionQuery.matches);
  }

  function onMedia(mq, fn) {
    if (!mq) {
      return;
    }
    if (mq.addEventListener) {
      mq.addEventListener('change', fn);
    } else if (mq.addListener) {
      mq.addListener(fn);
    }
  }

  function scrollTo(el) {
    if (!el || !el.scrollIntoView) {
      return;
    }
    try {
      el.scrollIntoView({block: 'center', behavior: reducedMotion() ? 'auto' : 'smooth'});
    } catch (e) {
      el.scrollIntoView();
    }
  }

  /* Focus without scrolling — used for panel internals that are already
   * on screen (a fixed panel must not drag the document around). */
  function focusOnly(el) {
    if (!el || !el.focus) {
      return;
    }
    if (!el.hasAttribute('tabindex') && !isNativelyFocusable(el)) {
      el.setAttribute('tabindex', '-1');
    }
    try {
      el.focus({preventScroll: true});
    } catch (e) {
      try {
        el.focus();
      } catch (e2) {
        /* nothing sensible left to do */
      }
    }
  }

  /* Focus and bring into view — used for keyboard jumps through the document. */
  function focusAndReveal(el) {
    focusOnly(el);
    scrollTo(el);
  }

  /* =======================================================================
   * 2. Live region
   * ---------------------------------------------------------------------
   * #rg-live is role="status" aria-live="polite" aria-atomic="true".
   * Contract rule: announce only when the change is NOT accompanied by a
   * focus move — a focus move announces the target already, and doubling up
   * is worse than silence.
   * ===================================================================== */

  var liveEl = null;
  var liveTimer = null;

  function announce(message) {
    if (!message) {
      return;
    }
    if (!liveEl) {
      liveEl = document.getElementById('rg-live');
    }
    if (!liveEl) {
      return;
    }
    /* Clearing first forces assistive tech to re-announce identical text. */
    window.clearTimeout(liveTimer);
    liveEl.textContent = '';
    liveTimer = window.setTimeout(function () {
      liveEl.textContent = message;
    }, 60);
  }

  function plural(n, one, many) {
    return n === 1 ? '1 ' + one : n + ' ' + many;
  }

  /* =======================================================================
   * 3. Status vocabulary — MUST stay in step with runs.py
   * ===================================================================== */

  var SECTION_LABEL = {
    pending: 'Waiting',
    running: 'Drafting…',
    retrying: 'Retrying',
    passed: 'Drafted · checks passed',
    failed: 'Drafted · checks failed',
    skipped: 'Not generated (deterministic/manual section)',
    cancelled: 'Cancelled'
  };

  /* Chip colour state per section status. This MUST mirror the same map in
   * the `progress_row` macro in base.html, or a row would change colour the
   * moment the first poll lands. A failed critique is a real quality problem,
   * so it reads as an error, not a soft warning. */
  var SECTION_STATE = {
    pending: 'neutral',
    running: 'running',
    retrying: 'warn',
    passed: 'ok',
    failed: 'error',
    skipped: 'neutral',
    cancelled: 'neutral'
  };

  var CHIP_STATES = ['ok', 'warn', 'error', 'running', 'neutral'];

  /* Colour never travels alone: when we swap a chip's colour we swap its
   * inline-SVG symbol to match. Sprite ids come from base.html. */
  var CHIP_ICON = {
    ok: 'check',
    warn: 'warn',
    error: 'alert',
    running: 'clock',
    neutral: 'dot'
  };

  var PHASE_ORDER = ['ingest', 'plan', 'draft', 'done'];

  /* =======================================================================
   * 4. Chip patching helpers
   * ---------------------------------------------------------------------
   * A status chip is `<span class="rg-chip rg-chip--x"><svg …/><span>label
   * </span></span>`. Setting textContent on the chip itself would delete the
   * icon and break the "every status colour is paired with an icon" rule, so
   * we patch only the label and leave the markup structure alone.
   * ===================================================================== */

  function chipLabelTarget(el) {
    var label = $('.rg-chip__label', el);
    if (label) {
      return label;
    }
    var kids = el.children || [];
    for (var i = kids.length - 1; i >= 0; i--) {
      var tag = (kids[i].tagName || '').toLowerCase();
      if (tag === 'span' && !kids[i].classList.contains('rg-icon')) {
        return kids[i];
      }
    }
    return null;
  }

  function setStatusText(el, label) {
    if (!el) {
      return;
    }
    var target = chipLabelTarget(el);
    if (target) {
      setText(target, label);
      return;
    }
    /* No wrapper span: patch text nodes in place so any sibling <svg> lives. */
    var textNodes = [];
    for (var node = el.firstChild; node; node = node.nextSibling) {
      if (node.nodeType === 3) {
        textNodes.push(node);
      }
    }
    var chosen = null;
    var i;
    for (i = 0; i < textNodes.length; i++) {
      if (textNodes[i].nodeValue.replace(/\s+/g, '')) {
        chosen = textNodes[i];
        break;
      }
    }
    if (!chosen && textNodes.length) {
      chosen = textNodes[textNodes.length - 1];
    }
    if (!chosen) {
      el.appendChild(document.createTextNode(label));
      return;
    }
    if (chosen.nodeValue !== label) {
      chosen.nodeValue = label;
    }
    for (i = 0; i < textNodes.length; i++) {
      if (textNodes[i] !== chosen && textNodes[i].nodeValue.replace(/\s+/g, '')) {
        textNodes[i].nodeValue = '';
      }
    }
  }

  function setChipState(host, state) {
    if (!host || CHIP_STATES.indexOf(state) === -1) {
      return;
    }
    /* The hook may be the chip itself or a wrapper around it. Modifier
     * classes must land on the element that actually carries `rg-chip`. */
    var el = host.classList.contains('rg-chip') ? host : $('.rg-chip', host) || host;

    var i;
    for (i = 0; i < CHIP_STATES.length; i++) {
      el.classList.remove('rg-chip--' + CHIP_STATES[i]);
    }
    el.classList.add('rg-chip--' + state);

    var symbol = '#i-' + (CHIP_ICON[state] || 'dot');
    var uses = $$('use', el);
    for (i = 0; i < uses.length; i++) {
      var href = uses[i].getAttribute('href') || uses[i].getAttribute('xlink:href') || '';
      /* Only retarget status symbols — never a source-type or decorative one. */
      if (href.indexOf('#i-') !== 0) {
        continue;
      }
      if (uses[i].hasAttribute('href')) {
        uses[i].setAttribute('href', symbol);
      }
      if (uses[i].hasAttribute('xlink:href')) {
        try {
          uses[i].setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', symbol);
        } catch (e) {
          /* namespaced attributes are optional */
        }
      }
    }

    /* Deliberately NOT adding an `rg-icon--{state}` modifier here: the
     * `progress_row` macro does not emit one either, and the chip's own
     * `rg-chip--{state}` already drives the colour through currentColor.
     * A JS-patched chip must be indistinguishable from a rendered one. */
  }

  /* Some counters are `<span id="…" class="rg-count__n">`, some wrap one. */
  function numberTarget(el) {
    return $('.rg-count__n', el) || el;
  }

  /* =======================================================================
   * 5. Time helpers
   * ===================================================================== */

  function parseIso(value) {
    if (!value) {
      return NaN;
    }
    var v = String(value).trim().replace(' ', 'T');
    /* runs.py emits UTC; tolerate a missing designator rather than drift 1h. */
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v) && !/(Z|[+-]\d{2}:?\d{2})$/.test(v)) {
      v += 'Z';
    }
    return Date.parse(v);
  }

  function pad2(n) {
    return (n < 10 ? '0' : '') + n;
  }

  function formatElapsed(seconds) {
    var s = Math.max(0, Math.floor(seconds));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    if (h > 0) {
      return h + ':' + pad2(m) + ':' + pad2(sec);
    }
    return pad2(m) + ':' + pad2(sec);
  }

  /* =======================================================================
   * 6. MODULE — progress polling
   * ---------------------------------------------------------------------
   * GET {data-poll-url} with If-None-Match. 304 -> nothing to do. 200 ->
   * patch and store the new ETag. Five consecutive failures -> stop the loop
   * and reveal #rg-poll-error rather than hammer a dead server forever.
   * Terminal -> full page reload exactly once; the server renders the draft,
   * we never render a report client-side.
   * ===================================================================== */

  var progress = (function () {
    var rootEl = null;
    var pollUrl = '';
    var etag = null;
    var timer = null;
    var failures = 0;
    var inFlight = false;
    var stopped = false;
    var reloaded = false;
    var delay = 1000;
    var lastStatusLabel = null;
    var seenFirstPayload = false;

    var MAX_FAILURES = 5;
    var REQUEST_TIMEOUT_MS = 15000;

    function init() {
      rootEl = document.getElementById('rg-progress');
      if (!rootEl) {
        return;
      }
      if (rootEl.getAttribute('data-terminal') !== 'false') {
        return;
      }
      pollUrl = rootEl.getAttribute('data-poll-url') || '';
      if (!pollUrl) {
        return;
      }

      if (!window.fetch || !window.Promise) {
        /* Ancient engine: degrade to the same behaviour the <noscript>
         * fallback gives everyone else. */
        window.setTimeout(function () {
          window.location.reload();
        }, 4000);
        return;
      }

      schedule(600);

      /* Coming back to a backgrounded tab should feel instant. */
      on(document, 'visibilitychange', function () {
        if (!stopped && !document.hidden) {
          schedule(0);
        }
      });
    }

    function schedule(ms) {
      if (stopped) {
        return;
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(poll, Math.max(0, ms));
    }

    function stop() {
      stopped = true;
      window.clearTimeout(timer);
    }

    function poll() {
      if (stopped || inFlight) {
        return;
      }
      inFlight = true;

      var headers = {Accept: 'application/json'};
      if (etag) {
        headers['If-None-Match'] = etag;
      }

      var controller = window.AbortController ? new window.AbortController() : null;
      var killer = controller
        ? window.setTimeout(function () {
            try {
              controller.abort();
            } catch (e) {
              /* already settled */
            }
          }, REQUEST_TIMEOUT_MS)
        : null;

      var opts = {
        headers: headers,
        cache: 'no-store',
        credentials: 'same-origin'
      };
      if (controller) {
        opts.signal = controller.signal;
      }

      window
        .fetch(pollUrl, opts)
        .then(function (res) {
          if (killer) {
            window.clearTimeout(killer);
          }
          if (res.status === 304) {
            return null;
          }
          if (!res.ok) {
            var err = new Error('HTTP ' + res.status);
            err.rgHttp = true;
            throw err;
          }
          var tag = res.headers.get('ETag');
          if (tag) {
            etag = tag;
          }
          return res.json();
        })
        .then(function (data) {
          inFlight = false;
          failures = 0;
          if (data) {
            handlePayload(data);
          } else {
            schedule(delay);
          }
        })
        ['catch'](function () {
          inFlight = false;
          onFailure();
        });
    }

    function onFailure() {
      failures += 1;
      if (failures >= MAX_FAILURES) {
        stop();
        var box = document.getElementById('rg-poll-error');
        if (box) {
          /* run.html gives this banner role="alert", so revealing it is
           * announced on its own. Adding a live-region message too would
           * say the same thing twice. */
          box.hidden = false;
        } else {
          announce('Live progress updates have stopped. Refresh the page to see the current status.');
        }
        return;
      }
      /* Gentle back-off so a flapping server is not hammered. */
      schedule(Math.min(8000, delay * (failures + 1)));
    }

    function handlePayload(data) {
      /* A patch bug must never kill the loop or, worse, the run page. */
      try {
        patch(data);
      } catch (e) {
        /* swallow: the server remains the source of truth */
      }

      if (data.terminal === true) {
        stop();
        elapsed.stop();
        if (!reloaded) {
          reloaded = true;
          window.location.reload();
        }
        return;
      }

      var next = typeof data.poll_after_ms === 'number' && data.poll_after_ms > 0
        ? data.poll_after_ms
        : 1000;
      delay = Math.max(250, next);
      schedule(delay);
    }

    function patch(data) {
      patchSections(data.sections || []);
      patchProgress(data.progress || {});
      patchPhase(data.phase, data.terminal === true);
      patchCounters(data);
      patchStartedAt(data.started_at);
      patchStatusLabel(data.status_label);
    }

    function patchSections(sections) {
      for (var i = 0; i < sections.length; i++) {
        var s = sections[i] || {};
        var id = s.section_id;
        if (id === undefined || id === null) {
          continue;
        }
        var row = null;
        var rows = $$('[data-section-row]');
        for (var j = 0; j < rows.length; j++) {
          if (rows[j].getAttribute('data-section-row') === String(id)) {
            row = rows[j];
            break;
          }
        }
        if (!row) {
          continue;
        }

        var statusEl = $('[data-role="status"]', row);
        if (statusEl) {
          var label = s.status_label || SECTION_LABEL[s.status] || SECTION_LABEL.pending;
          setStatusText(statusEl, label);
          setChipState(statusEl, SECTION_STATE[s.status] || 'neutral');
        }

        var citeEl = $('[data-role="citations"]', row);
        if (citeEl) {
          var n = typeof s.n_citations === 'number' ? s.n_citations : 0;
          setText(citeEl, plural(n, 'citation', 'citations'));
        }

        var attemptsEl = $('[data-role="attempts"]', row);
        if (attemptsEl) {
          var attempts = typeof s.attempts === 'number' ? s.attempts : 0;
          var showAttempts = attempts > 1 || s.status === 'retrying';
          setText(attemptsEl, showAttempts ? 'attempt ' + Math.max(attempts, 1) + ' of 2' : '');
        }
      }
    }

    function patchProgress(p) {
      var total = typeof p.sections_total === 'number' ? p.sections_total : 0;
      var done = typeof p.sections_done === 'number' ? p.sections_done : 0;
      var percent = typeof p.percent === 'number' ? p.percent : 0;
      percent = Math.max(0, Math.min(100, percent));

      var bar = document.getElementById('rg-progress-bar');
      if (bar) {
        var fill = $('.rg-bar__fill', bar);
        if (fill) {
          fill.style.width = percent + '%';
        }
        bar.setAttribute('aria-valuenow', String(percent));
        bar.setAttribute('aria-valuetext', done + ' of ' + total + ' sections');
      }

      setText(document.getElementById('rg-progress-count'), done + ' of ' + total + ' sections');

      var failedEl = document.getElementById('rg-counter-failed');
      if (failedEl && typeof p.sections_failed === 'number') {
        setText(numberTarget(failedEl), String(p.sections_failed));
      }
    }

    function patchPhase(phase, terminal) {
      var host = document.getElementById('rg-phase');
      if (!host) {
        return;
      }
      var steps = $$('[data-phase]', host);
      if (!steps.length) {
        return;
      }
      var names = steps.map(function (el) {
        return el.getAttribute('data-phase');
      });
      var current = names.indexOf(phase);
      if (current === -1) {
        current = PHASE_ORDER.indexOf(phase);
      }
      if (current === -1) {
        return;
      }
      if (terminal) {
        current = steps.length - 1;
      }
      for (var i = 0; i < steps.length; i++) {
        var el = steps[i];
        var isDone = i < current || (terminal && i <= current);
        var isCurrent = i === current;
        /* Both spellings are managed: `rg-phase__step--current` is what the
         * server renders on first paint, `is-current` is the JS-owned state
         * class. Keeping them in step avoids two "current" markers at once. */
        if (isDone) {
          el.classList.add('rg-phase__step--done');
        } else {
          el.classList.remove('rg-phase__step--done');
        }
        if (isCurrent) {
          el.classList.add('is-current');
          el.classList.add('rg-phase__step--current');
          el.setAttribute('aria-current', 'step');
        } else {
          el.classList.remove('is-current');
          el.classList.remove('rg-phase__step--current');
          el.removeAttribute('aria-current');
        }
      }
    }

    function patchCounters(data) {
      var totals = data.totals || {};
      var citations = document.getElementById('rg-counter-citations');
      if (citations && typeof totals.citations === 'number') {
        setText(numberTarget(citations), String(totals.citations));
      }
      var failed = document.getElementById('rg-counter-failed');
      if (failed && typeof totals.failed === 'number') {
        setText(numberTarget(failed), String(totals.failed));
      }
    }

    function patchStartedAt(startedAt) {
      if (!startedAt) {
        return;
      }
      var el = document.getElementById('rg-elapsed');
      if (el && !el.getAttribute('data-started-at')) {
        el.setAttribute('data-started-at', startedAt);
        elapsed.restart();
      }
    }

    function patchStatusLabel(label) {
      if (!label) {
        return;
      }
      /* First payload only primes the value: the server already rendered the
       * same words, so announcing them would be an echo. */
      if (!seenFirstPayload) {
        seenFirstPayload = true;
        lastStatusLabel = label;
        return;
      }
      if (label !== lastStatusLabel) {
        lastStatusLabel = label;
        announce(label);
      }
    }

    return {init: init, stop: stop};
  })();

  /* =======================================================================
   * 7. MODULE — elapsed clock
   * ---------------------------------------------------------------------
   * Under prefers-reduced-motion we still tell the truth, just less often:
   * a once-per-second changing region is motion.
   * ===================================================================== */

  var elapsed = (function () {
    var el = null;
    var timer = null;
    var startMs = NaN;

    function render() {
      if (isNaN(startMs)) {
        return;
      }
      setText(el, formatElapsed((Date.now() - startMs) / 1000));
    }

    function start() {
      window.clearInterval(timer);
      if (!el) {
        return;
      }
      startMs = parseIso(el.getAttribute('data-started-at'));
      if (isNaN(startMs)) {
        return;
      }
      var finished = el.getAttribute('data-finished-at');
      if (finished) {
        var endMs = parseIso(finished);
        if (!isNaN(endMs)) {
          setText(el, formatElapsed((endMs - startMs) / 1000));
          return;
        }
      }
      render();
      timer = window.setInterval(render, reducedMotion() ? 5000 : 1000);
    }

    function init() {
      el = document.getElementById('rg-elapsed');
      if (!el) {
        return;
      }
      start();
      onMedia(motionQuery, start);
    }

    return {
      init: init,
      restart: start,
      stop: function () {
        window.clearInterval(timer);
      }
    };
  })();

  /* =======================================================================
   * 8. MODULE — citation inspector
   * ---------------------------------------------------------------------
   * There is no citation fetch and no client-side citation markup. The full
   * reference appendix (#rg-refs > article#ref-N) is server-rendered once;
   * pinning clones the matching <article> into the panel. That is what makes
   * the no-JS fallback, the deep link (#ref-4) and the panel one single
   * source of truth — the panel can never disagree with the appendix.
   * ===================================================================== */

  var inspector = (function () {
    var panel = null;
    var panelBody = null;
    var panelTitle = null;
    var refsHost = null;
    var markers = [];
    var refArticles = [];
    var emptyState = [];

    var pinnedMarker = null;
    var pinnedId = '';
    var activeClaim = null;
    var relatedClaims = [];
    var peeked = [];

    var narrowQuery = window.matchMedia ? window.matchMedia('(max-width: 1023.98px)') : null;

    /* ---- lookups ---------------------------------------------------- */

    function citeIdOf(el) {
      return (el && el.getAttribute('data-citation-id')) || '';
    }

    function citeNumberOf(marker) {
      var n = marker.getAttribute('data-cite-n');
      if (n) {
        return n;
      }
      var href = marker.getAttribute('href') || '';
      var m = /#ref-(.+)$/.exec(href);
      return m ? m[1] : '';
    }

    function refFor(marker) {
      var n = citeNumberOf(marker);
      if (!n) {
        return null;
      }
      return document.getElementById('ref-' + n);
    }

    /* Attribute values are opaque ids; filtering in JS avoids any need to
     * escape them into a selector. */
    function markersWithId(id) {
      if (!id) {
        return [];
      }
      return markers.filter(function (m) {
        return citeIdOf(m) === id;
      });
    }

    function refsWithId(id) {
      if (!id) {
        return [];
      }
      return refArticles.filter(function (r) {
        return citeIdOf(r) === id;
      });
    }

    /* Which claim does this marker belong to?
     *
     * The contract pins down the whitespace ("…</span><a class="rg-cite"…>")
     * but not whether the marker is nested inside `.rg-claim` or chained
     * immediately after it, and run.html is owned by another engineer. Both
     * shapes are therefore supported:
     *
     *   nested   <span class="rg-claim"><span class="rg-claim__text">…</span><a class="rg-cite">…</a></span>
     *   chained  <span class="rg-claim">…</span><a class="rg-cite">…</a>
     *
     * Anything else simply yields no claim, and highlighting degrades to
     * nothing rather than to something wrong. */
    function claimBefore(node) {
      var prev = node ? node.previousElementSibling : null;
      while (prev) {
        if (!prev.classList) {
          return null;
        }
        if (prev.classList.contains('rg-claim')) {
          return prev;
        }
        /* Step back over adjacent markers for a multi-citation claim. */
        if (prev.classList.contains('rg-cite') || prev.classList.contains('rg-cite-group')) {
          prev = prev.previousElementSibling;
          continue;
        }
        return null;
      }
      return null;
    }

    function claimOf(marker) {
      var nested = closestOf(marker, '.rg-claim');
      if (nested) {
        return nested;
      }
      return claimBefore(closestOf(marker, '.rg-cite-group') || marker);
    }

    /* ---- clone hygiene ----------------------------------------------- */

    /* The appendix article stays in the document, so the clone must not
     * duplicate its ids. Rename them and repoint every intra-clone
     * reference, otherwise `getElementById` and every aria-*-by association
     * silently resolves to the wrong node. */
    function namespaceIds(root, prefix) {
      var map = {};
      var withIds = [root].concat($$('[id]', root));
      withIds.forEach(function (el) {
        if (!el.id) {
          return;
        }
        map[el.id] = prefix + el.id;
        el.id = prefix + el.id;
      });

      var refAttrs = [
        'for',
        'aria-labelledby',
        'aria-describedby',
        'aria-controls',
        'aria-owns',
        'aria-activedescendant',
        'headers'
      ];
      var all = [root].concat($$('*', root));
      all.forEach(function (el) {
        if (!el.getAttribute) {
          return;
        }
        refAttrs.forEach(function (attr) {
          var value = el.getAttribute(attr);
          if (!value) {
            return;
          }
          var mapped = value.split(/\s+/).map(function (token) {
            return Object.prototype.hasOwnProperty.call(map, token) ? map[token] : token;
          });
          el.setAttribute(attr, mapped.join(' '));
        });
        var href = el.getAttribute('href');
        if (href && href.charAt(0) === '#') {
          var target = href.slice(1);
          if (Object.prototype.hasOwnProperty.call(map, target)) {
            el.setAttribute('href', '#' + map[target]);
          }
        }
      });
    }

    function replaceKids(parent, nodes) {
      if (!parent) {
        return;
      }
      while (parent.firstChild) {
        parent.removeChild(parent.firstChild);
      }
      for (var i = 0; i < nodes.length; i++) {
        parent.appendChild(nodes[i]);
      }
    }

    /* ---- highlighting ------------------------------------------------ */

    function clearRelated() {
      relatedClaims.forEach(function (el) {
        el.classList.remove('is-related');
      });
      relatedClaims = [];
    }

    function applyRelated(citationId, exceptClaim) {
      clearRelated();
      if (!citationId) {
        return;
      }
      markersWithId(citationId).forEach(function (m) {
        var claim = claimOf(m);
        if (!claim || claim === exceptClaim) {
          return;
        }
        if (relatedClaims.indexOf(claim) === -1) {
          claim.classList.add('is-related');
          relatedClaims.push(claim);
        }
      });
    }

    function clearPeek() {
      peeked.forEach(function (el) {
        el.classList.remove('is-peeking');
      });
      peeked = [];
    }

    function applyPeek(citationId) {
      clearPeek();
      refsWithId(citationId).forEach(function (r) {
        r.classList.add('is-peeking');
        peeked.push(r);
      });
    }

    /* Hover/focus preview of the relation graph; leaving restores whatever
     * the pinned citation had highlighted. */
    function hoverEnter(citationId) {
      applyRelated(citationId, activeClaim);
      applyPeek(citationId);
    }

    function hoverLeave() {
      clearPeek();
      applyRelated(pinnedId, activeClaim);
    }

    /* ---- modal behaviour under 1024px -------------------------------- */

    function isNarrow() {
      return !!(narrowQuery && narrowQuery.matches);
    }

    function syncModal() {
      if (!panel) {
        return;
      }
      if (!panel.hidden && isNarrow()) {
        panel.setAttribute('role', 'dialog');
        panel.setAttribute('aria-modal', 'true');
      } else {
        panel.setAttribute('role', 'complementary');
        panel.removeAttribute('aria-modal');
      }
    }

    function trapTab(event) {
      if (event.key !== 'Tab' || !panel || panel.hidden || !isNarrow()) {
        return;
      }
      var items = focusablesIn(panel);
      if (!items.length) {
        event.preventDefault();
        focusOnly(panelTitle);
        return;
      }
      var first = items[0];
      var last = items[items.length - 1];
      var active = document.activeElement;
      if (!panel.contains(active)) {
        event.preventDefault();
        focusOnly(event.shiftKey ? last : first);
        return;
      }
      if (event.shiftKey && (active === first || active === panelTitle)) {
        event.preventDefault();
        focusOnly(last);
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        focusOnly(first);
      }
    }

    /* ---- pin / unpin -------------------------------------------------- */

    function releaseMarker() {
      if (pinnedMarker) {
        pinnedMarker.classList.remove('is-pinned');
        pinnedMarker.setAttribute('aria-expanded', 'false');
      }
      if (activeClaim) {
        activeClaim.classList.remove('is-active');
      }
      clearRelated();
      clearPeek();
      pinnedMarker = null;
      pinnedId = '';
      activeClaim = null;
    }

    function pin(marker) {
      if (!panel || !panelBody || !marker) {
        return false;
      }
      var source = refFor(marker);
      if (!source) {
        /* Nothing to show — let the browser do its native anchor jump. */
        return false;
      }

      releaseMarker();

      var clone = source.cloneNode(true);
      namespaceIds(clone, 'rgi-');
      replaceKids(panelBody, [clone]);

      panel.hidden = false;
      marker.classList.add('is-pinned');
      marker.setAttribute('aria-expanded', 'true');
      pinnedMarker = marker;
      pinnedId = citeIdOf(marker);

      activeClaim = claimOf(marker);
      if (activeClaim) {
        activeClaim.classList.add('is-active');
      }
      applyRelated(pinnedId, activeClaim);

      syncModal();
      /* Focus moves into the panel, so the heading is announced natively —
       * no live-region message here (doubling is worse than silence). */
      focusOnly(panelTitle);
      return true;
    }

    function unpin(returnFocus) {
      if (!panel) {
        return false;
      }
      if (panel.hidden && !pinnedMarker) {
        return false;
      }
      var origin = pinnedMarker;
      releaseMarker();
      panel.hidden = true;
      syncModal();
      replaceKids(
        panelBody,
        emptyState.map(function (node) {
          return node.cloneNode(true);
        })
      );
      if (returnFocus && origin) {
        focusOnly(origin);
      }
      return true;
    }

    function toggle(marker) {
      if (marker && marker === pinnedMarker) {
        return unpin(true);
      }
      return pin(marker);
    }

    /* ---- boot --------------------------------------------------------- */

    function init() {
      panel = document.getElementById('rg-inspector');
      panelBody = document.getElementById('rg-inspector-body');
      panelTitle = document.getElementById('rg-inspector-title');
      refsHost = document.getElementById('rg-refs');
      markers = $$('a.rg-cite');
      refArticles = refsHost ? $$('article.rg-ref', refsHost) : [];

      if (!markers.length) {
        return;
      }

      /* Cross-highlighting works even without a panel (draft-only view). */
      markers.forEach(function (m) {
        on(m, 'mouseenter', function () {
          hoverEnter(citeIdOf(m));
        });
        on(m, 'mouseleave', hoverLeave);
        on(m, 'focus', function () {
          hoverEnter(citeIdOf(m));
        });
        on(m, 'blur', hoverLeave);
      });

      refArticles.forEach(function (r) {
        on(r, 'mouseenter', function () {
          hoverEnter(citeIdOf(r));
        });
        on(r, 'mouseleave', hoverLeave);
        on(
          r,
          'focusin',
          function () {
            hoverEnter(citeIdOf(r));
          },
          false
        );
        on(r, 'focusout', hoverLeave, false);
      });

      if (!panel || !panelBody) {
        return;
      }

      emptyState = Array.prototype.map.call(panelBody.childNodes, function (node) {
        return node.cloneNode(true);
      });

      markers.forEach(function (m) {
        m.setAttribute('aria-controls', 'rg-inspector');
        m.setAttribute('aria-expanded', 'false');
        on(m, 'click', function (event) {
          if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) {
            return; /* let "open in new tab" and friends work */
          }
          if (pin(m)) {
            event.preventDefault();
          }
        });
        /* Space activates a <button>; on an <a> it scrolls. Emulate it. */
        on(m, 'keydown', function (event) {
          if (event.key === ' ' || event.key === 'Spacebar' || event.keyCode === 32) {
            event.preventDefault();
            pin(m);
          }
        });
      });

      on(panel, 'click', function (event) {
        var trigger = closestOf(event.target, '[data-action="unpin"], #rg-inspector-close');
        if (!trigger) {
          return;
        }
        event.preventDefault();
        unpin(true);
      });

      on(document, 'keydown', trapTab, true);
      onMedia(narrowQuery, syncModal);
      syncModal();
    }

    return {
      init: init,
      pin: pin,
      unpin: unpin,
      toggle: toggle,
      isOpen: function () {
        return !!(panel && !panel.hidden);
      },
      pinnedMarker: function () {
        return pinnedMarker;
      },
      markers: function () {
        return markers;
      }
    };
  })();

  /* =======================================================================
   * 9. MODULE — display + filter switches
   * ---------------------------------------------------------------------
   * JS rewrites two attributes on #rg-app. Every pixel of the resulting
   * change is CSS's job — we never hide, restyle or rebuild content here.
   * ===================================================================== */

  var DISPLAY_ANNOUNCE = {
    markers: 'Citation markers shown inline.',
    quiet: 'Citation markers hidden. Numbers remain in the references appendix.',
    audit: 'Audit view. Full citation detail shown inline.'
  };

  var FILTER_ANNOUNCE = {
    all: 'Showing the whole draft.',
    flagged: 'Showing flagged content only: failed sections and uncited numbers.'
  };

  function initSwitches() {
    var app = document.getElementById('rg-app');
    if (!app) {
      return;
    }
    wireSwitch(app, 'data-set-display', 'data-cite-display', 'markers', DISPLAY_ANNOUNCE);
    wireSwitch(app, 'data-set-filter', 'data-filter', 'all', FILTER_ANNOUNCE);
  }

  function wireSwitch(app, triggerAttr, stateAttr, fallback, messages) {
    var buttons = $$('[' + triggerAttr + ']');
    if (!buttons.length) {
      return;
    }

    function sync() {
      var current = app.getAttribute(stateAttr) || fallback;
      buttons.forEach(function (btn) {
        btn.setAttribute('aria-pressed', btn.getAttribute(triggerAttr) === current ? 'true' : 'false');
      });
    }

    buttons.forEach(function (btn) {
      on(btn, 'click', function (event) {
        event.preventDefault();
        var value = btn.getAttribute(triggerAttr);
        if (!value) {
          return;
        }
        app.setAttribute(stateAttr, value);
        sync();
        /* Focus stays on the button, so the change itself needs announcing. */
        announce(messages[value] || '');
      });
    });

    sync();
  }

  /* =======================================================================
   * 10. MODULE — copy to clipboard
   * ===================================================================== */

  function legacyCopy(text) {
    var previous = document.activeElement;
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.setAttribute('aria-hidden', 'true');
    ta.setAttribute('tabindex', '-1');
    ta.style.position = 'fixed';
    ta.style.top = '0';
    ta.style.left = '-9999px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    var ok = false;
    try {
      ta.select();
      if (ta.setSelectionRange) {
        ta.setSelectionRange(0, ta.value.length);
      }
      ok = document.execCommand('copy');
    } catch (e) {
      ok = false;
    }
    document.body.removeChild(ta);
    if (previous && previous !== document.body && previous.focus) {
      try {
        previous.focus({preventScroll: true});
      } catch (e) {
        previous.focus();
      }
    }
    return ok;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText && window.Promise) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy(text);
        }
      );
    }
    var ok = legacyCopy(text);
    return window.Promise ? window.Promise.resolve(ok) : {then: function (fn) { fn(ok); }};
  }

  function initCopy() {
    on(document, 'click', function (event) {
      var trigger = closestOf(event.target, '[data-copy]');
      if (!trigger) {
        return;
      }
      event.preventDefault();
      var text = trigger.getAttribute('data-copy') || '';
      if (!text) {
        return;
      }
      trigger.classList.add('is-active');
      window.setTimeout(function () {
        trigger.classList.remove('is-active');
      }, 1400);
      copyText(text).then(function (ok) {
        announce(
          ok
            ? 'Copied to the clipboard.'
            : 'Copy failed. Select the text and press Control C.'
        );
      });
    });
  }

  /* =======================================================================
   * 11. MODULE — keyboard shortcuts
   * ---------------------------------------------------------------------
   * WCAG 2.1.4: every single-character shortcut can be switched off with
   * #rg-shortcuts-toggle, and every one of them is also a visible control in
   * .rg-reviewbar__controls. Escape is a non-printable key and therefore
   * exempt, so it always works.
   * ===================================================================== */

  function shortcutsEnabled() {
    var toggle = document.getElementById('rg-shortcuts-toggle');
    return !toggle || toggle.checked !== false;
  }

  function isTypingTarget(el) {
    if (!el) {
      return false;
    }
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      return true;
    }
    return !!el.isContentEditable;
  }

  var cursors = {cite: -1, flag: -1};

  function cycle(list, dir, cursorKey, emptyMessage) {
    var items = list.filter(isVisible);
    if (!items.length) {
      announce(emptyMessage);
      return;
    }
    var index = items.indexOf(document.activeElement);
    if (index === -1) {
      index = cursors[cursorKey];
      if (index >= items.length) {
        index = -1;
      }
    }
    var next;
    if (index === -1) {
      next = dir > 0 ? 0 : items.length - 1;
    } else {
      next = (index + dir + items.length) % items.length;
    }
    cursors[cursorKey] = next;
    focusAndReveal(items[next]);
  }

  function flaggedItems() {
    return $$('.rg-section--failed, .rg-uncited, .rg-claim--uncited');
  }

  function initShortcuts() {
    on(document, 'keydown', function (event) {
      if (event.defaultPrevented) {
        return;
      }

      /* Escape first: it must work from inside the panel, and from a field. */
      if (event.key === 'Escape' || event.key === 'Esc') {
        if (inspector.isOpen()) {
          event.preventDefault();
          inspector.unpin(true);
        }
        return;
      }

      if (event.ctrlKey || event.metaKey || event.altKey) {
        return;
      }
      if (isTypingTarget(document.activeElement)) {
        return;
      }
      if (!shortcutsEnabled()) {
        return;
      }

      var key = event.key;

      if (key === '/') {
        var filter = document.getElementById('rg-run-filter');
        if (filter) {
          event.preventDefault();
          focusOnly(filter);
          if (filter.select) {
            filter.select();
          }
        }
        return;
      }

      if (key === 'c' || key === 'C') {
        event.preventDefault();
        cycle(inspector.markers(), event.shiftKey ? -1 : 1, 'cite', 'No citation markers in this draft.');
        return;
      }

      if (key === 'f' || key === 'F') {
        event.preventDefault();
        cycle(flaggedItems(), event.shiftKey ? -1 : 1, 'flag', 'Nothing is flagged in this draft.');
        return;
      }

      if (key === 'i' || key === 'I') {
        var active = document.activeElement;
        if (active && active.classList && active.classList.contains('rg-cite')) {
          event.preventDefault();
          inspector.toggle(active);
        } else if (inspector.isOpen()) {
          event.preventDefault();
          inspector.unpin(true);
        }
        return;
      }

      if (key === 'r' || key === 'R') {
        var refs = document.getElementById('rg-refs');
        if (refs) {
          event.preventDefault();
          focusAndReveal($('.rg-refs__title', refs) || refs);
        }
        return;
      }
    });
  }

  /* =======================================================================
   * 12. MODULE — run history filter (gallery, view="runs")
   * ---------------------------------------------------------------------
   * Pure client-side narrowing of an already-complete server-rendered table.
   * ===================================================================== */

  function initRunFilter() {
    var input = document.getElementById('rg-run-filter');
    var table = document.getElementById('rg-run-table');
    if (!input || !table) {
      return;
    }
    var rows = $$('tr[data-search]', table);
    if (!rows.length) {
      return;
    }

    var announceTimer = null;

    function apply(quiet) {
      var q = (input.value || '').trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var hay = row.getAttribute('data-search') || '';
        var hit = !q || hay.indexOf(q) !== -1;
        if (hit) {
          row.classList.remove('is-hidden');
          shown += 1;
        } else {
          row.classList.add('is-hidden');
        }
      });
      if (quiet) {
        return;
      }
      /* Focus stays in the field while typing, so the result count would
       * otherwise be invisible to a screen-reader user. Debounced so we do
       * not narrate every keystroke. */
      window.clearTimeout(announceTimer);
      announceTimer = window.setTimeout(function () {
        announce(
          shown === rows.length
            ? plural(shown, 'run shown', 'runs shown')
            : shown + ' of ' + plural(rows.length, 'run matches', 'runs match')
        );
      }, 600);
    }

    on(input, 'input', function () {
      apply(false);
    });
    on(input, 'search', function () {
      apply(false);
    });

    if ((input.value || '').trim()) {
      apply(true);
    }
  }

  /* =======================================================================
   * 13. MODULE — double-submit guard
   * ---------------------------------------------------------------------
   * POST /runs starts a real generation run. A double click must not start
   * two. We never set the `disabled` property (a disabled control is a dead
   * control and drops its name/value) — aria-disabled plus swallowing the
   * second submit event is enough.
   * ===================================================================== */

  function clearBusy() {
    $$('form[data-rg-busy]').forEach(function (form) {
      form.removeAttribute('data-rg-busy');
    });
    $$('.is-busy').forEach(function (el) {
      el.classList.remove('is-busy');
      el.removeAttribute('aria-disabled');
    });
  }

  function initForms() {
    on(
      document,
      'submit',
      function (event) {
        var form = event.target;
        if (!form || form.tagName !== 'FORM') {
          return;
        }
        if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') {
          return;
        }
        if (form.getAttribute('data-rg-busy') === '1') {
          event.preventDefault();
          return;
        }
        form.setAttribute('data-rg-busy', '1');
        var btn =
          event.submitter ||
          $('button[type="submit"], button:not([type]), input[type="submit"]', form);
        if (btn && btn.classList) {
          btn.classList.add('is-busy');
          btn.setAttribute('aria-disabled', 'true');
        }
        /* If the navigation never happens (blocked, cancelled), give the
         * control back rather than stranding the user on a dead button. */
        window.setTimeout(clearBusy, 12000);
      },
      true
    );

    /* Back/forward cache restores the DOM exactly as it was left. */
    on(window, 'pageshow', clearBusy);
  }

  /* =======================================================================
   * 14. Boot
   * ===================================================================== */

  function safely(fn) {
    try {
      fn();
    } catch (e) {
      /* One broken module must not take the others down with it. */
      if (window.console && window.console.warn) {
        window.console.warn('[rg] module failed to start', e);
      }
    }
  }

  function boot() {
    var app = document.getElementById('rg-app');
    if (app) {
      app.setAttribute('data-js', 'on');
    }
    safely(inspector.init);
    safely(initSwitches);
    safely(initCopy);
    safely(initShortcuts);
    safely(initRunFilter);
    safely(initForms);
    safely(elapsed.init);
    safely(progress.init);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
