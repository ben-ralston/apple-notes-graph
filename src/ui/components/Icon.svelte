<script>
  // The app's one icon size scale and stroke weight, over Lucide glyphs.
  const SIZES = { sm: 14, md: 16, lg: 20 };

  let { icon: Glyph, size = 'md' } = $props();
</script>

<Glyph size={SIZES[size]} strokeWidth={1.75} aria-hidden="true" style="flex-shrink: 0" />
