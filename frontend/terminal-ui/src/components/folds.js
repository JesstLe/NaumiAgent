export const CODE_FOLD_VISIBLE_LINES = 40;
export const DIFF_FOLD_VISIBLE_LINES = 60;

export function createFoldState() {
  return {};
}

export function isFoldExpanded(folds, key) {
  return Boolean(key && folds?.[key]?.expanded === true);
}

export function setFoldExpanded(folds, key, expanded) {
  if (!key) return folds;
  return {
    ...folds,
    [key]: { ...(folds?.[key] ?? {}), expanded: Boolean(expanded) },
  };
}

export function foldLines(lines, { folds, key, visibleLines, hiddenLabel }) {
  if (isFoldExpanded(folds, key) || lines.length <= visibleLines) {
    return { lines, hiddenCount: 0, notice: "" };
  }
  const hiddenCount = lines.length - visibleLines;
  return {
    lines: lines.slice(0, visibleLines),
    hiddenCount,
    notice: `... 已折叠 ${hiddenCount} 行${hiddenLabel}`,
  };
}
