// common namespace — app-wide chrome shared across surfaces (document title,
// language toggle, generic verbs). OWNED by the i18n foundation; surface
// fan-out agents must NOT edit this file (they add keys to their own
// namespace). See docs/plans/2026-07-11-i18n-japanese-localization-design.md.
export const common = {
  en: {
    // The runtime document.title (the store syncs it on every locale change).
    'common.documentTitle': 'DriftScribe — the agent proposes, you approve',
    // Language toggle (LocaleToggle.svelte).
    'common.localeToggle.aria': 'Language',
    'common.localeToggle.selectEnglish': 'Switch to English',
    'common.localeToggle.selectJapanese': 'Switch to Japanese',
    // Generic verbs reused across surfaces.
    'common.close': 'Close',
    'common.cancel': 'Cancel',
    'common.retry': 'Retry',
    'common.loading': 'Loading…',
  },
  ja: {
    'common.documentTitle': 'DriftScribe：エージェントが提案し、あなたが承認',
    'common.localeToggle.aria': '言語',
    'common.localeToggle.selectEnglish': '英語に切り替える',
    'common.localeToggle.selectJapanese': '日本語に切り替える',
    'common.close': '閉じる',
    'common.cancel': 'キャンセル',
    'common.retry': '再試行',
    'common.loading': '読み込み中…',
  },
};
