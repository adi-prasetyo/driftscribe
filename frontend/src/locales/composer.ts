// composer namespace — ChatForm and JA crew descriptors/summaries
// (workloads.catalog.json is backend-pinned EN; JA lives here keyed by workload).
//
// The old crew-PICKER keys stay gone, and the distinction matters. That picker
// made the operator declare a specialist before they had said what they wanted,
// and greyed out three of four cards mid-thread with a lock hint explaining the
// refusal. Neither is back: `composer.crewMenu.*` below labels a control that
// starts closed, defaults to Explore, and offers every crew an ACTION rather
// than a refusal — so there is still no "choose a crew" legend and no lock hint.
//
// What is back is the crew's NAME on screen (ds-uyo). #255 removed the display
// along with the control, and an Adopt click could arm Provision without ever
// saying so.
//
// `newChat` left for a different reason (ds-jns PR 3): starting a thread belongs
// beside the list of threads, not beside the box you type in. It is now
// `conversations.rail.newChat`.
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
    'composer.chatForm.send': 'Send',

    // CrewMenu.svelte — the crew display/control at the leading edge of the
    // composer. The rows themselves cost no new strings (descriptor, lifecycle
    // and summary already live in `shared.crew.*` in both languages); the
    // CHROME does, which is these four.
    //
    // The trigger's visible text is the crew name alone, so the accessible name
    // has to supply both halves of what the control is: the state it reports and
    // the thing it does.
    'composer.crewMenu.triggerAriaLabel': 'Crew: {crew}. Change crew.',
    'composer.crewMenu.listAriaLabel': 'Choose a crew',
    // Visible text beside the check on the selected row. A tick alone leans on
    // shape to carry meaning; aria-selected covers assistive tech but not the
    // sighted operator who has to work out what a lone ✓ is claiming.
    'composer.crewMenu.current': 'current',
    // Said BEFORE the click, on every non-current row while a thread is open.
    // Deliberately not "switch" or "hand over": a handoff continues this thread
    // with its context, and this does not — it opens a clean one. The thread
    // being left is not destroyed; its id lives in /conversations.
    'composer.crewMenu.startsNewChat': 'starts new chat',
  },
  ja: {
    'composer.chatForm.placeholder':
      'コーディネーターに質問…（Enter で送信・Shift+Enter で改行）',
    'composer.chatForm.promptAriaLabel': 'プロンプト',
    'composer.chatForm.enterShiftHint':
      'Enter で送信します。Shift+Enter で改行します。',
    'composer.chatForm.send': '送信',

    'composer.crewMenu.triggerAriaLabel': 'クルー：{crew}。クルーを変更します。',
    'composer.crewMenu.listAriaLabel': 'クルーを選択',
    'composer.crewMenu.current': '選択中',
    'composer.crewMenu.startsNewChat': '新しいチャットを開始',
  },
};
