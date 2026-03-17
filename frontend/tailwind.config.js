/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg:       '#FBF7F0',   // warm cream — morning light on paper
        surface:  '#FFFFFF',   // pure white cards
        border:   '#E8DDD0',   // warm sand
        'border-deep': '#D4C4B0',
        teal:     '#0D9488',   // bullish (kept as requested)
        red:      '#DC2626',   // bearish (kept as requested)
        amber:    '#D97706',   // golden neutral
        gold:     '#C8860A',   // hero accent — deep golden
        muted:    '#A89585',   // light warm captions
        text:     '#2C1810',   // deep warm brown
        'text-2': '#7A6355',   // medium warm labels
        nav:      '#2C1810',   // top bar background
      },
      fontFamily: {
        display: ['"Playfair Display"', 'Georgia', 'serif'],
        sans:    ['"Source Sans 3"', 'sans-serif'],
        mono:    ['"IBM Plex Mono"', 'monospace'],
      },
      boxShadow: {
        card:  '0 1px 4px rgba(44,24,16,0.06), 0 4px 16px rgba(44,24,16,0.04)',
        'card-hover': '0 2px 8px rgba(44,24,16,0.10), 0 8px 24px rgba(44,24,16,0.06)',
        nav:   '0 1px 0 rgba(44,24,16,0.12)',
      },
    },
  },
  plugins: [],
}
