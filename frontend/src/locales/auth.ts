// auth namespace — AuthPanel, TokenStatus.
export const auth = {
  en: {
    // AuthPanel.svelte — modal title (reused as the input's aria-label).
    'auth.panel.title': 'Operator token',
    // Description sentence split around the literal <code>sessionStorage</code>
    // element (not translated — a JS API name). Concatenated at render time:
    // descBefore + "sessionStorage" + descAfter.
    'auth.panel.descBefore': 'Stored in ',
    'auth.panel.descAfter':
      ' for this tab only. Cleared when you close the tab, never sent anywhere but the coordinator.',
    'auth.panel.placeholder': 'Paste your operator token…',
    'auth.panel.cancel': 'Cancel',
    'auth.panel.save': 'Save',
    // TokenStatus.svelte — pill label per TokenState, plus the tertiary link.
    'auth.status.ok': 'token ok',
    'auth.status.missing': 'no token',
    'auth.status.invalid': 'token rejected',
    'auth.status.changeToken': 'change token',
  },
  ja: {
    'auth.panel.title': 'オペレーター用トークン',
    'auth.panel.descBefore': 'このタブの ',
    'auth.panel.descAfter':
      ' にのみ保存されます。タブを閉じると消去され、コーディネーター以外には送信されません。',
    'auth.panel.placeholder': 'オペレーター用トークンを貼り付け…',
    'auth.panel.cancel': 'キャンセル',
    'auth.panel.save': '保存',
    'auth.status.ok': 'トークン有効',
    'auth.status.missing': 'トークン未設定',
    'auth.status.invalid': 'トークン無効',
    'auth.status.changeToken': 'トークンを変更',
  },
};
