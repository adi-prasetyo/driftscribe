<script lang="ts">
  import { ICON_PATHS, type IconName } from '../lib/icons';
  let { name, size = 16, extraClass = '' }: { name: IconName; size?: number; extraClass?: string } = $props();
  const cls = $derived(extraClass ? `ds-icon ${extraClass}` : 'ds-icon');
</script>
<svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false" class={cls}>{@html ICON_PATHS[name]}</svg>
