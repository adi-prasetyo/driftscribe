// chat namespace — the empty new-chat state (ds-jns PR 3).
//
// The chips are the front door for a visitor who has never met this agent. The
// text a chip carries is EXACTLY the text it drops in the composer: no short
// label hiding a longer hidden prompt, so nothing appears in the box that the
// operator did not read before clicking. They are editable once there, and
// nothing sends until Send.
//
// One chip per crew's flavour, but NONE of them names a crew and none switches
// the composer's workload — a fresh thread goes to Explore, whose whole job is
// to work out which specialist a question belongs to. Naming the crews here
// would put the system's own taxonomy back on the front door, which is the
// thing the crew picker was removed for.
export const chat = {
  en: {
    'chat.empty.greeting': 'What should we look at?',
    'chat.empty.chipsAriaLabel': 'Example questions',
    // Explore: read-only inventory. First because it is the broadest question
    // and the one a first-time visitor is most likely to actually have.
    'chat.empty.chip.explore': 'What is running in this project right now?',
    // Anchor: Cloud Run config drift against the checked-in definition.
    'chat.empty.chip.anchor': 'Does the running Cloud Run config still match the code?',
    // Patch: dependency versions against the GitHub Advisory database.
    'chat.empty.chip.patch': 'Are any of our dependencies affected by a known advisory?',
    // Provision: authors the OpenTofu that would adopt an unmanaged resource.
    'chat.empty.chip.provision': 'Which resources here are not managed by OpenTofu yet?',
  },
  ja: {
    'chat.empty.greeting': '何を確認しますか？',
    'chat.empty.chipsAriaLabel': '質問の例',
    'chat.empty.chip.explore': 'このプロジェクトでいま動いているものを教えてください。',
    'chat.empty.chip.anchor': '本番の Cloud Run 設定は、コードの内容と一致していますか？',
    'chat.empty.chip.patch': '依存パッケージに、既知の脆弱性はありますか？',
    'chat.empty.chip.provision': 'まだ OpenTofu で管理していないリソースはどれですか？',
  },
};
