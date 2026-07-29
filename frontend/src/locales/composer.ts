// composer namespace — ChatForm and JA crew descriptors/summaries
// (workloads.catalog.json is backend-pinned EN; JA lives here keyed by workload).
//
// The crew-picker keys are gone with the picker itself: the operator no longer
// chooses a crew before typing, so there is no "choose a crew" legend and no
// crew-lock hint to explain why the other three are greyed out. The lock is
// still real — it is just no longer something the operator has to work around,
// because a crew that needs a sibling now offers the handoff itself.
export const composer = {
  en: {
    // ChatForm.svelte — the prompt composer.
    'composer.chatForm.placeholder':
      'Ask the coordinator…  (Enter to send · Shift+Enter for a new line)',
    'composer.chatForm.promptAriaLabel': 'Prompt',
    // Same Enter/Shift+Enter guidance as the placeholder, but for assistive
    // tech (the placeholder itself vanishes once typing starts).
    'composer.chatForm.enterShiftHint':
      'Press Enter to send. Press Shift plus Enter for a new line.',
    'composer.chatForm.newChat': 'New chat',
    'composer.chatForm.send': 'Send',
  },
  ja: {
    'composer.chatForm.placeholder':
      'コーディネーターに質問…（Enter で送信・Shift+Enter で改行）',
    'composer.chatForm.promptAriaLabel': 'プロンプト',
    'composer.chatForm.enterShiftHint':
      'Enter で送信します。Shift+Enter で改行します。',
    'composer.chatForm.newChat': '新規チャット',
    'composer.chatForm.send': '送信',
  },
};
