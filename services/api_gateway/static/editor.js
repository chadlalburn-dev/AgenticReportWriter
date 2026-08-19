/* ===========================================================================
 * Report Generator Agent — template editor behaviour
 * ---------------------------------------------------------------------------
 * Owner: ENG-5.  Loaded ONLY from template_editor.html as:
 *     <script src="/static/editor.js" defer></script>
 * Single IIFE, vanilla ES5-flavoured JS. No modules, no bundler, no imports,
 * no fetch, no off-origin requests of any kind.
 *
 * EVERYTHING HERE IS A PROGRESSIVE ENHANCEMENT (contract section 7.2).
 * Three structural rules make no-JS parity real rather than aspirational:
 *   1. <template> contents are inert — not submitted, not validated, and they
 *      cannot collide on id. That is exactly why it is the right element.
 *   2. Row keys are OPAQUE. This file never renumbers a key and never has to
 *      agree with the server about indices.
 *   3. Every structural button is a real submit carrying formnovalidate. We
 *      preventDefault() them; delete this file and every flow still works,
 *      because the server handles the identical `op` value.
 *
 * What this file is allowed to write (contract section 7.2):
 *   `is-` prefixed classes, the `hidden` attribute, text nodes, and
 *   value/name attributes on nodes it just created. It never removes a
 *   server-rendered structural class.
 *   Two documented exceptions, both required behaviours from section 7.2.2:
 *     - `textarea.rows` for auto-grow (an attribute, no CSS available to us);
 *     - `option.disabled` on options this file just created.
 *
 * The load-bearing guarantee this file exists to provide:
 *   every section's source picker is kept identical to the sources actually
 *   defined in the form, so a reference to a source that does not exist is
 *   UNREACHABLE rather than merely rejected after the fact.
 * ======================================================================== */

(function () {
  'use strict';

  /* =======================================================================
   * 0. Tiny DOM helpers (same shapes as app.js, deliberately duplicated —
   *    app.js is frozen and exports nothing).
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

  function plural(n, one, many) {
    return n === 1 ? '1 ' + one : n + ' ' + many;
  }

  /* =======================================================================
   * 1. Boot guard
   * ===================================================================== */

  var form = document.getElementById('rg-editor');
  if (!form || !form.hasAttribute('data-template-editor')) {
    return;
  }

  /* =======================================================================
   * 2. Live region
   * ---------------------------------------------------------------------
   * app.js keeps announce() inside its own closure, so we write #rg-live
   * ourselves using the same clear-then-set pattern (contract section 7.2).
   * Announce only when the change is NOT accompanied by a focus move.
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
    window.clearTimeout(liveTimer);
    liveEl.textContent = '';
    liveTimer = window.setTimeout(function () {
      liveEl.textContent = message;
    }, 60);
  }

  /* =======================================================================
   * 3. Vocabulary
   * ===================================================================== */

  var GROUPS = ['input', 'source', 'section'];

  var NOUN = {input: 'input', source: 'source', section: 'section'};

  var EMPTY_NAME = {
    input: 'not named yet',
    source: 'not named yet',
    section: 'not titled yet'
  };

  /* Kind -> human word, read out of the "New source type" select so this file
   * and the server can never disagree about the wording. */
  var kindWords = null;

  function kindWord(kind) {
    if (!kindWords) {
      kindWords = {};
      var chooser = $('[name="add_source_kind"]', form);
      $$('option', chooser).forEach(function (opt) {
        kindWords[opt.value] = (opt.textContent || '').replace(/\s+/g, ' ').trim();
      });
    }
    return kindWords[kind] || kind;
  }

  /* =======================================================================
   * 4. Row plumbing
   * ===================================================================== */

  function containerFor(group) {
    return document.getElementById('rg-rows-' + group);
  }

  function rowsOf(group) {
    var container = containerFor(group);
    if (!container) {
      return [];
    }
    return $$('[data-row][data-group="' + group + '"]', container);
  }

  function keyOf(row) {
    return (row && row.getAttribute('data-key')) || '';
  }

  /* The field whose value names the row: `.id` for inputs and sources,
   * `.heading` for sections. `query_id` / `page_id` end in `_id`, not `.id`,
   * so the suffix match cannot pick them up by accident. */
  function nameFieldOf(row) {
    return $('[name$=".id"]', row) || $('[name$=".heading"]', row);
  }

  var keySeq = 0;

  function freshKey() {
    var used = {};
    $$('[data-row]', form).forEach(function (row) {
      used[keyOf(row)] = true;
    });
    var candidate;
    do {
      keySeq += 1;
      candidate = ('j' + Date.now().toString(36) + keySeq.toString(36)).slice(0, 16);
    } while (used[candidate]);
    return candidate;
  }

  /* Clone a blueprint. Replacing the tokens in the markup string rewrites
   * every name, id, for, aria-describedby and data-key in one pass, so a row
   * can never end up half-renamed. Every node returned is one we just made. */
  function cloneFromTemplate(templateId, key, sectionKey) {
    var tpl = document.getElementById(templateId);
    if (!tpl) {
      return null;
    }
    var markup = tpl.innerHTML;
    if (!markup) {
      return null;
    }
    markup = markup.split('__SECTION__').join(sectionKey || key);
    markup = markup.split('__KEY__').join(key);
    var holder = document.createElement('div');
    holder.innerHTML = markup;
    return holder.firstElementChild;
  }

  function emptyNoteOf(group) {
    var container = containerFor(group);
    return container ? $('[data-empty-note]', container) : null;
  }

  function refreshEmptyNote(group) {
    var note = emptyNoteOf(group);
    if (note) {
      note.hidden = rowsOf(group).length > 0;
    }
  }

  function focusFirstField(row) {
    var first = $('input:not([type="hidden"]), select, textarea', row);
    if (first && first.focus) {
      try {
        first.focus();
      } catch (e) {
        /* focus is a nicety, never a failure */
      }
    }
  }

  /* =======================================================================
   * 5. The live picture of the sources
   * ===================================================================== */

  function sourceInfo() {
    return rowsOf('source').map(function (row) {
      var idEl = $('[data-source-id]', row);
      var kindEl = $('[data-source-kind]', row);
      return {
        key: keyOf(row),
        id: idEl && idEl.value ? idEl.value.trim() : '',
        kind: kindEl && kindEl.value ? kindEl.value : 'bigquery'
      };
    }).filter(function (info) {
      return info.key !== '';
    });
  }

  /* Text only. Safe to run on every keystroke: it moves nothing, so it can
   * never steal focus or reorder anything under the user's hands. */
  function refreshLabels() {
    sourceInfo().forEach(function (info) {
      $$('[data-source-label="' + info.key + '"]', form).forEach(function (el) {
        setText(el, info.id || 'unnamed');
      });
      $$('[data-source-kindword="' + info.key + '"]', form).forEach(function (el) {
        setText(el, kindWord(info.kind));
      });
    });
    GROUPS.forEach(function (group) {
      rowsOf(group).forEach(function (row) {
        var legendId = $('[data-legend-id]', row);
        if (!legendId) {
          return;
        }
        var field = nameFieldOf(row);
        var text = field && field.value ? field.value.trim() : '';
        setText(legendId, text || EMPTY_NAME[group]);
      });
    });
  }

  /* Positions are server-owned on first render and ours afterwards. The
   * number lives in a text node in the legend and in a visually hidden text
   * node inside each row button, so keeping it truthful costs only setText. */
  function renumber(group) {
    rowsOf(group).forEach(function (row, index) {
      var position = index + 1;
      setText($('[data-legend-number]', row), String(position));
      $$('[data-act-position]', row).forEach(function (el) {
        setText(el, ', ' + NOUN[group] + ' ' + position);
      });
    });
  }

  /* ---------------------------------------------------------------------
   * THE INVARIANT: every section's picker lists exactly the live sources,
   * in the order they are defined. Checkboxes for removed sources are
   * deleted rather than merely unchecked, so an unknown-source reference
   * cannot be submitted at all.
   * ------------------------------------------------------------------- */
  function syncPickers() {
    var sources = sourceInfo();
    var live = {};
    sources.forEach(function (info) {
      live[info.key] = info;
    });

    $$('[data-sources-picker]', form).forEach(function (picker) {
      var sectionKey = picker.getAttribute('data-section') || '';

      /* 1. drop references to sources that no longer exist */
      $$('[data-source-ref]', picker).forEach(function (input) {
        if (!live[input.getAttribute('data-source-ref')]) {
          var stale = closestOf(input, '.rg-checkgroup__item') || input.parentNode;
          if (stale && stale.parentNode) {
            stale.parentNode.removeChild(stale);
          }
        }
      });

      /* 2. add the missing ones and put them all in source order */
      sources.forEach(function (info) {
        var input = $('[data-source-ref="' + info.key + '"]', picker);
        var item;
        if (input) {
          item = closestOf(input, '.rg-checkgroup__item') || input.parentNode;
        } else {
          item = cloneFromTemplate('rg-tpl-source-ref', info.key, sectionKey);
        }
        if (!item) {
          return;
        }
        /* appendChild on a node already in the picker MOVES it — checkedness
         * is a property and survives the move, which cloning would not. */
        picker.appendChild(item);
        setText($('[data-source-label]', item), info.id || 'unnamed');
        setText($('[data-source-kindword]', item), kindWord(info.kind));
      });
    });
  }

  /* Table options mirror the same list. Only data queries can back a table,
   * so every other kind is offered but disabled — the reason stays visible
   * instead of the option silently not being there. */
  function syncTables() {
    var sources = sourceInfo();

    $$('[data-table-select]', form).forEach(function (select) {
      var current = select.value;
      var stillValid = false;
      sources.forEach(function (info) {
        if (info.key === current && info.kind === 'bigquery') {
          stillValid = true;
        }
      });

      var blankLabel = 'No table';
      if (select.options.length && select.options[0].value === '') {
        blankLabel = select.options[0].textContent;
      }
      while (select.options.length) {
        select.remove(0);
      }

      var blank = document.createElement('option');
      blank.value = '';
      blank.textContent = blankLabel;
      select.appendChild(blank);

      sources.forEach(function (info) {
        var option = document.createElement('option');
        option.value = info.key;
        option.textContent = (info.id || 'unnamed') + ' — ' + kindWord(info.kind);
        if (info.kind !== 'bigquery') {
          option.disabled = true;
        }
        select.appendChild(option);
      });

      select.value = stillValid ? current : '';
    });
  }

  function syncSourceReferences() {
    syncPickers();
    syncTables();
  }

  /* =======================================================================
   * 6. Source kind switching
   * ===================================================================== */

  function toggleKinds(row) {
    var select = $('[data-source-kind]', row);
    if (!select) {
      return;
    }
    var kind = select.value;
    $$('[data-kind]', row).forEach(function (fieldset) {
      fieldset.hidden = fieldset.getAttribute('data-kind') !== kind;
    });
  }

  /* =======================================================================
   * 7. Structural operations
   * ===================================================================== */

  var dirty = false;
  var submitting = false;

  function addRow(group) {
    var container = containerFor(group);
    if (!container) {
      return;
    }
    var key = freshKey();
    var row = cloneFromTemplate('rg-tpl-row-' + group, key, key);
    if (!row) {
      return;
    }
    container.appendChild(row);

    if (group === 'source') {
      var chooser = $('[name="add_source_kind"]', form);
      var kindSelect = $('[data-source-kind]', row);
      if (chooser && kindSelect) {
        kindSelect.value = chooser.value;
      }
      toggleKinds(row);
    }

    refreshEmptyNote(group);
    renumber(group);
    refreshLabels();
    if (group === 'source' || group === 'section') {
      syncSourceReferences();
    }
    dirty = true;

    focusFirstField(row);
    announce('Added ' + NOUN[group] + ' ' + rowsOf(group).length + '. Nothing is written until you press Save template.');
  }

  function removeRow(row) {
    var group = row.getAttribute('data-group');
    if (!group) {
      return;
    }
    var before = rowsOf(group);
    var index = before.indexOf(row);
    var name = (function () {
      var field = nameFieldOf(row);
      return field && field.value ? field.value.trim() : '';
    }());

    if (row.parentNode) {
      row.parentNode.removeChild(row);
    }

    var after = rowsOf(group);
    refreshEmptyNote(group);
    renumber(group);
    refreshLabels();
    if (group === 'source') {
      syncSourceReferences();
    }
    dirty = true;

    /* The button that was pressed no longer exists, so focus has to be put
     * somewhere deliberate or it falls back to <body>. */
    var neighbour = after[Math.min(index, after.length - 1)];
    var target = neighbour ? $('[data-act="remove"]', neighbour) : $('[data-add="' + group + '"]', form);
    if (target && target.focus) {
      target.focus();
    }

    var what = name ? NOUN[group] + ' "' + name + '"' : NOUN[group] + ' ' + (index + 1);
    var tail = group === 'source'
      ? ' Any section that used it no longer refers to it.'
      : '';
    announce('Removed ' + what + '.' + tail + ' ' + plural(after.length, NOUN[group] + ' left', NOUN[group] + 's left') + '.');
  }

  function siblingRow(row, direction) {
    var node = direction === 'up' ? row.previousElementSibling : row.nextElementSibling;
    while (node && !(node.hasAttribute && node.hasAttribute('data-row'))) {
      node = direction === 'up' ? node.previousElementSibling : node.nextElementSibling;
    }
    return node;
  }

  function moveRow(row, direction, button) {
    var group = row.getAttribute('data-group');
    var neighbour = siblingRow(row, direction);
    if (!group || !neighbour || !row.parentNode) {
      announce(NOUN[group || 'input'] + ' is already ' + (direction === 'up' ? 'first' : 'last') + '.');
      return;
    }

    if (direction === 'up') {
      row.parentNode.insertBefore(row, neighbour);
    } else {
      row.parentNode.insertBefore(neighbour, row);
    }

    renumber(group);
    if (group === 'source') {
      /* picker order follows source order */
      syncPickers();
    }
    dirty = true;

    /* Moving a node can drop focus, so put it back on the control that was
     * pressed — a user reordering with the keyboard must not lose their place. */
    if (button && button.focus) {
      try {
        button.focus();
      } catch (e) {
        /* not fatal */
      }
    }

    var position = rowsOf(group).indexOf(row) + 1;
    announce(NOUN[group].charAt(0).toUpperCase() + NOUN[group].slice(1) +
             ' moved to position ' + position + ' of ' + rowsOf(group).length + '.');
  }

  /* =======================================================================
   * 8. Auto-grow
   * ---------------------------------------------------------------------
   * Instruction and SQL fields routinely outgrow their box. We cannot add a
   * CSS rule (gsk.css belongs to ENG-3) and we do not write inline styles,
   * so we grow the textarea the declarative way, via its rows attribute.
   * ===================================================================== */

  var MAX_ROWS = 24;

  function autoGrow(el) {
    if (!el || el.tagName !== 'TEXTAREA') {
      return;
    }
    var floor = parseInt(el.getAttribute('data-min-rows') || '', 10);
    if (!floor || floor < 1) {
      floor = el.rows || 3;
      el.setAttribute('data-min-rows', String(floor));
    }
    var lines = (el.value || '').split('\n').length;
    var wanted = Math.max(floor, Math.min(MAX_ROWS, lines + 1));
    if (el.rows !== wanted) {
      el.rows = wanted;
    }
  }

  /* =======================================================================
   * 9. Wiring
   * ===================================================================== */

  on(form, 'click', function (ev) {
    var button = closestOf(ev.target, 'button[data-add], button[data-act]');
    if (!button || button.disabled) {
      return;
    }

    var add = button.getAttribute('data-add');
    if (add) {
      ev.preventDefault();
      addRow(add);
      return;
    }

    var act = button.getAttribute('data-act');
    if (!act) {
      return;
    }
    var row = closestOf(button, '[data-row]');
    if (!row) {
      return;
    }
    ev.preventDefault();
    if (act === 'remove') {
      removeRow(row);
    } else if (act === 'up' || act === 'down') {
      moveRow(row, act, button);
    }
  });

  on(form, 'change', function (ev) {
    var target = ev.target;
    if (!target) {
      return;
    }
    dirty = true;

    if (target.hasAttribute && target.hasAttribute('data-source-kind')) {
      var row = closestOf(target, '[data-row]');
      if (row) {
        toggleKinds(row);
      }
      refreshLabels();
      /* A source that is no longer a data query cannot back a table. */
      syncTables();
      announce('Source type changed to ' + kindWord(target.value) + '. The fields below it have been swapped.');
    }
  });

  on(form, 'input', function (ev) {
    var target = ev.target;
    if (!target) {
      return;
    }
    dirty = true;

    if (target.hasAttribute && target.hasAttribute('data-source-id')) {
      /* Row keys are the reference, never the id, so a rename propagates for
       * free — we only repaint the words a human reads. */
      refreshLabels();
      syncTables();
    }
    if (target.tagName === 'TEXTAREA') {
      autoGrow(target);
    }
  });

  on(form, 'submit', function () {
    submitting = true;
  });

  on(window, 'beforeunload', function (ev) {
    if (!dirty || submitting) {
      return undefined;
    }
    ev.preventDefault();
    ev.returnValue = '';
    return '';
  });

  /* =======================================================================
   * 10. Boot
   * ---------------------------------------------------------------------
   * Deliberately NOT calling syncSourceReferences() here. On first paint the
   * server is the authority: if it rendered a table pointing at a source that
   * is not a data query, that is an error it is reporting, and silently
   * resetting the field would erase both the value and the explanation.
   * ===================================================================== */

  $$('[data-nojs-only]', form).forEach(function (el) {
    el.hidden = true;
  });

  rowsOf('source').forEach(toggleKinds);
  $$('textarea', form).forEach(autoGrow);
}());
