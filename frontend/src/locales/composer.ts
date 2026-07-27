// composer namespace — ChatForm, CrewPicker, and JA crew descriptors/summaries
// (workloads.catalog.json is backend-pinned EN; JA lives here keyed by workload).
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
    // CrewPicker.svelte — the four crew cards + crew-lock hint.
    'composer.crewPicker.legend': 'Choose a crew member',
    // `{crew}` is the crewName() proper noun (e.g. "Anchor") — kept out of the
    // catalog, interpolated at render time.
    'composer.crewPicker.lockHint':
      'This thread is with {crew}. Start a new chat to switch crews.',
  },
  ja: {
    'composer.chatForm.placeholder':
      'コーディネーターに質問…（Enter で送信・Shift+Enter で改行）',
    'composer.chatForm.promptAriaLabel': 'プロンプト',
    'composer.chatForm.enterShiftHint':
      'Enter で送信します。Shift+Enter で改行します。',
    'composer.chatForm.newChat': '新規チャット',
    'composer.chatForm.send': '送信',
    'composer.crewPicker.legend': 'エージェントチームを選択',
    'composer.crewPicker.lockHint':
      'このスレッドは{crew}が担当しています。エージェントチームを切り替えるには、新規チャットを開始してください。',
  },
};
