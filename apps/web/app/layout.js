import "./globals.css";
import { I18nProvider } from "../lib/i18n";

export const metadata = {
  title: "KM VMS",
  description: "Система видеонаблюдения"
};

export default function RootLayout({ children }) {
  return (
    <html lang="ru">
      <body>
        <I18nProvider>{children}</I18nProvider>
      </body>
    </html>
  );
}
