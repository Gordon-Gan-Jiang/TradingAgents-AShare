/** @type {import('tailwindcss').Config} */
export default {
    darkMode: 'class',
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                trading: {
                    bg: {
                        primary: '#0D1117',
                        secondary: '#161B22',
                        tertiary: '#21262D',
                    },
                    accent: {
                        green: '#238636',
                        red: '#DA3633',
                        blue: '#58A6FF',
                        orange: '#F0883E',
                        purple: '#8957E5',
                        cyan: '#39D0D8',
                    },
                    text: {
                        primary: '#E6EDF3',
                        secondary: '#8B949E',
                        muted: '#6E7681',
                    },
                    border: '#30363D',
                },
            },
            fontFamily: {
                mono: ['JetBrains Mono', 'SF Mono', 'monospace'],
                sans: ['Inter', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
            },
            animation: {
                'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'spin-slow': 'spin 3s linear infinite',
            },
        },
    },
    plugins: [],
}