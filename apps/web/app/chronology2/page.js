import { redirect } from "next/navigation";

export default function ChronologyCompatibilityRedirectPage() {
  // Temporary compatibility route for bookmarks created before the production
  // chronology route was renamed to /chronology.
  redirect("/chronology");
}
