/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg:          '#0A0E27',
        surface:     '#0F1535',
        surfaceHi:   '#162040',
        border:      '#1E2A4A',
        text:        '#E8EAED',
        muted:       '#6B7280',
        accent:      '#00D9FF',
        success:     '#00FF88',
        danger:      '#FF0055',
        warning:     '#FFB800',
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"Fira Code"', 'Consolas', 'monospace'],
      },
      keyframes: {
        'slide-in': {
          from: { opacity: '0', transform: 'translateY(-8px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        'glow-pulse': {
          '0%, 100%': { boxShadow: '0 0 4px rgba(0,217,255,0.3)' },
          '50%':      { boxShadow: '0 0 16px rgba(0,217,255,0.7)' },
        },
      },
      animation: {
        'slide-in':   'slide-in 0.25s ease-out',
        'glow-pulse': 'glow-pulse 2s ease-in-out infinite',
      },
    },
  },
  plugins: [],
}
