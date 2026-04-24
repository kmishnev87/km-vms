import "./globals.css";

export const metadata = {
  title: "KM VMS",
  description: "Система видеонаблюдения"
};

export default function RootLayout({ children }) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
