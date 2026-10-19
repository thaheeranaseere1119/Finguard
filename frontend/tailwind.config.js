/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"] },
      colors: {
        brand: { 50: "#eef4ff", 100: "#dbe6fe", 500: "#3b6ff6", 600: "#2856e3", 700: "#1f44b8", 900: "#16245c" },
      },
    },
  },
  plugins: [],
};
