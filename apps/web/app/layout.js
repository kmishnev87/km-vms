import "./globals.css";

export const metadata = {
  title: "TNAS VMS",
  description: "Система видеонаблюдения"
};

export default function RootLayout({ children }) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
