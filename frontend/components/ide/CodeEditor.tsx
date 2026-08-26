"use client";

/**
 * The editor both consoles are built on.
 *
 * CodeMirror 6 rather than Monaco. Monaco is the better editor in isolation --
 * it is VS Code's -- and the wrong one here: it ships around 2MB, wants web
 * workers configured by hand, and fights the App Router's server rendering
 * because it touches `window` at module scope. CodeMirror is roughly a tenth of
 * that, has a first-party SQL mode that takes a live schema for completion, and
 * is composed of extensions rather than configured through options, which is
 * what makes the honey theme below a few lines instead of a fork.
 *
 * Everything CodeMirror owns lives behind `viewRef` and is created exactly once
 * per mount. React re-renders are not allowed to rebuild it: an editor that
 * loses the cursor because a sibling's state changed is unusable, so the two
 * things that legitimately change after mount -- the language (with its schema)
 * and the run shortcut -- go through compartments instead.
 */

import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { EditorState, Compartment } from "@codemirror/state";
import { EditorView, keymap, placeholder as placeholderExt } from "@codemirror/view";
import { basicSetup } from "codemirror";
import { indentWithTab } from "@codemirror/commands";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { PostgreSQL, sql } from "@codemirror/lang-sql";
import { python } from "@codemirror/lang-python";
import { tags } from "@lezer/highlight";

export type EditorLanguage = "sql" | "python";

/** Table name -> column names. What @codemirror/lang-sql wants for completion. */
export type SqlSchema = Record<string, string[]>;

export interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  language: EditorLanguage;
  /** SQL only. Feeding it the source's real tables is what makes completion useful. */
  schema?: SqlSchema;
  /** Bound to Ctrl/Cmd-Enter. Read through a ref so the binding never goes stale. */
  onRun?: () => void;
  placeholder?: string;
  ariaLabel: string;
}

/**
 * What a parent can ask the editor to do.
 *
 * Deliberately two methods. Anything richer -- reading the document, setting
 * the selection -- would be React state kept in two places, and the `value`
 * prop already covers it. `insert` exists because "put this table name where
 * the cursor is" cannot be expressed as a value change without guessing where
 * the cursor was.
 */
export interface CodeEditorHandle {
  insert: (text: string) => void;
  focus: () => void;
  /**
   * The highlighted text, or "" when nothing is highlighted.
   *
   * A SQL console lives on this: a scratch buffer holds five statements and
   * Run means "the one I have selected". Reading it on demand rather than
   * mirroring the selection into React state keeps a re-render off every
   * cursor move.
   */
  getSelection: () => string;
}

/* Syntax colours, drawn from the brand palette rather than from a stock theme.
   Amber carries keywords because it is the product's own accent; strings and
   numbers take the two greens so a quoted date never reads as a keyword. The
   greens are the only colours here that are not already in globals.css --
   syntax highlighting needs hues the brand does not have, and inventing two is
   better than making one amber mean four different things. */
const KEYWORD = "#a86a12";
const STRING = "#3f7d3a";
const NUMBER = "#2f6f6b";
const COMMENT = "#9a9377";
const NAME = "#312f17";
const FUNCTION = "#8a5a9e";

const honeyHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: KEYWORD, fontWeight: "600" },
  { tag: tags.operatorKeyword, color: KEYWORD, fontWeight: "600" },
  { tag: tags.controlKeyword, color: KEYWORD, fontWeight: "600" },
  { tag: tags.definitionKeyword, color: KEYWORD, fontWeight: "600" },
  { tag: tags.modifier, color: KEYWORD },
  { tag: [tags.string, tags.special(tags.string)], color: STRING },
  { tag: [tags.number, tags.bool, tags.null], color: NUMBER },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: COMMENT, fontStyle: "italic" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: FUNCTION },
  { tag: tags.typeName, color: NUMBER },
  { tag: [tags.variableName, tags.propertyName], color: NAME },
  { tag: tags.definition(tags.variableName), color: NAME, fontWeight: "600" },
  { tag: tags.operator, color: "#7a7357" },
  { tag: tags.punctuation, color: "#7a7357" },
  { tag: tags.invalid, color: "#9b3d22" },
]);

/* The chrome: gutters, selection, the completion popup. Kept in one theme
   object so there is a single place the editor's surface is described, and it
   reads from the same variables the rest of the dashboard does. */
const honeyTheme = EditorView.theme(
  {
    "&": {
      height: "100%",
      fontSize: "13.5px",
      backgroundColor: "var(--bg)",
      color: "var(--ink)",
    },
    "&.cm-focused": { outline: "none" },
    ".cm-scroller": {
      // The same stack the dashboard's other code surfaces use. No webfont:
      // the app loads none, and naming one that is not there means the editor
      // silently renders in whatever the fallback happens to be.
      fontFamily:
        'ui-monospace, "Cascadia Mono", "Segoe UI Mono", "Roboto Mono", monospace',
      lineHeight: "1.65",
      padding: "12px 0",
    },
    ".cm-content": { padding: "0 4px", caretColor: "var(--accent-hover)" },
    ".cm-gutters": {
      backgroundColor: "transparent",
      borderRight: "1px solid var(--border)",
      color: "#b6ac8e",
      paddingRight: "2px",
    },
    ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--muted)" },
    // A tinted band rather than a border: a 1px outline on the active line
    // shifts the text by a pixel as the cursor moves, which reads as jitter.
    ".cm-activeLine": { backgroundColor: "rgba(234, 157, 62, 0.07)" },
    ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
      backgroundColor: "rgba(234, 157, 62, 0.24)",
    },
    ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--accent-hover)", borderLeftWidth: "2px" },
    ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": {
      backgroundColor: "rgba(234, 157, 62, 0.28)",
      outline: "none",
    },
    ".cm-tooltip": {
      border: "1px solid var(--border)",
      backgroundColor: "#fffdf8",
      borderRadius: "8px",
      boxShadow: "0 10px 30px rgba(49, 47, 23, 0.13)",
      overflow: "hidden",
    },
    ".cm-tooltip-autocomplete > ul": {
      fontFamily:
        'ui-monospace, "Cascadia Mono", "Segoe UI Mono", "Roboto Mono", monospace',
      fontSize: "12.5px",
      maxHeight: "220px",
    },
    ".cm-tooltip-autocomplete > ul > li": { padding: "4px 10px" },
    ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
      backgroundColor: "var(--accent)",
      color: "var(--ink)",
    },
    ".cm-completionIcon": { paddingRight: "14px", opacity: 0.6 },
    ".cm-completionDetail": { color: "var(--muted)", fontStyle: "normal", marginLeft: "10px" },
    ".cm-panels": { backgroundColor: "#fffdf8", color: "var(--ink)" },
    ".cm-panels.cm-panels-bottom": { borderTop: "1px solid var(--border)" },
    ".cm-searchMatch": { backgroundColor: "rgba(229, 189, 63, 0.4)" },
    ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "rgba(234, 157, 62, 0.6)" },
    ".cm-placeholder": { color: "#b6ac8e", fontStyle: "normal" },
  },
  { dark: false }
);

function languageExtension(language: EditorLanguage, schema?: SqlSchema) {
  if (language === "python") {
    return python();
  }
  return sql({
    dialect: PostgreSQL,
    // An empty object is not the same as undefined here: it means "this source
    // has no tables I know of", which is the honest state before the schema
    // request lands, and stops completion offering stale names.
    schema: schema || {},
    upperCaseKeywords: false,
  });
}

function CodeEditor(
  { value, onChange, language, schema, onRun, placeholder, ariaLabel }: CodeEditorProps,
  ref: React.ForwardedRef<CodeEditorHandle>
) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const languageCompartment = useRef(new Compartment());

  // The callbacks are read out of refs inside the extensions. Closing over the
  // props directly would freeze the first render's versions into an editor
  // that is deliberately never rebuilt.
  const onChangeRef = useRef(onChange);
  const onRunRef = useRef(onRun);
  onChangeRef.current = onChange;
  onRunRef.current = onRun;

  useEffect(
    function mountEditor() {
      const host = hostRef.current;
      if (host === null) {
        return;
      }

      const view = new EditorView({
        parent: host,
        state: EditorState.create({
          doc: value,
          extensions: [
            basicSetup,
            // After basicSetup so it overrides that bundle's default highlight
            // style; CodeMirror resolves conflicting extensions by precedence,
            // and later wins.
            syntaxHighlighting(honeyHighlight),
            honeyTheme,
            keymap.of([
              {
                key: "Mod-Enter",
                run: function runShortcut() {
                  const handler = onRunRef.current;
                  if (handler === undefined) {
                    return false;
                  }
                  handler();
                  return true;
                },
              },
              // Tab indents rather than leaving the editor. The accessibility
              // trade is real -- Escape then Tab still moves focus out, which
              // is the escape hatch the guideline asks for.
              indentWithTab,
            ]),
            languageCompartment.current.of(languageExtension(language, schema)),
            placeholderExt(placeholder || ""),
            EditorView.updateListener.of(function onUpdate(update) {
              if (update.docChanged) {
                onChangeRef.current(update.state.doc.toString());
              }
            }),
            EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
          ],
        }),
      });
      viewRef.current = view;

      return function unmount() {
        view.destroy();
        viewRef.current = null;
      };
    },
    // Mount once. `value` is intentionally not a dependency -- see the sync
    // effect below, which pushes external changes in without a rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );

  useEffect(
    function syncLanguage() {
      const view = viewRef.current;
      if (view === null) {
        return;
      }
      view.dispatch({
        effects: languageCompartment.current.reconfigure(
          languageExtension(language, schema)
        ),
      });
    },
    [language, schema]
  );

  useEffect(
    function syncValue() {
      const view = viewRef.current;
      if (view === null) {
        return;
      }
      const current = view.state.doc.toString();
      // Guarding on equality is what stops a feedback loop: every keystroke
      // calls onChange, the parent sets state, and this effect runs again. If
      // it dispatched unconditionally it would replace the document under the
      // cursor on every character typed.
      if (current === value) {
        return;
      }
      view.dispatch({
        changes: { from: 0, to: current.length, insert: value },
        // Put the cursor at the end of what was just loaded rather than
        // leaving it at an offset from the previous document.
        selection: { anchor: value.length },
      });
    },
    [value]
  );

  useImperativeHandle(
    ref,
    function api() {
      return {
        insert: function insert(text: string): void {
          const view = viewRef.current;
          if (view === null) {
            return;
          }
          const range = view.state.selection.main;
          // Replace the selection rather than always appending: picking a
          // table name while a placeholder is highlighted should swap it,
          // which is what every other editor does.
          view.dispatch({
            changes: { from: range.from, to: range.to, insert: text },
            selection: { anchor: range.from + text.length },
            scrollIntoView: true,
          });
          view.focus();
        },
        focus: function focus(): void {
          if (viewRef.current !== null) {
            viewRef.current.focus();
          }
        },
        getSelection: function getSelection(): string {
          const view = viewRef.current;
          if (view === null) {
            return "";
          }
          const range = view.state.selection.main;
          if (range.empty) {
            return "";
          }
          return view.state.sliceDoc(range.from, range.to);
        },
      };
    },
    []
  );

  return <div className="ide-editor" ref={hostRef} />;
}

export default forwardRef<CodeEditorHandle, CodeEditorProps>(CodeEditor);
