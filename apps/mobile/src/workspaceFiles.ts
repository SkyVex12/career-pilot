import * as DocumentPicker from "expo-document-picker";
import { File, Paths } from "expo-file-system";

export type WorkspaceFile = {
  name: string;
  contentType: string;
  bytes: Uint8Array;
};
/** Reads only a file explicitly selected in the system picker. No library-wide access. */
export async function pickWorkspaceFile(
  pdf = false,
): Promise<WorkspaceFile | null> {
  const picked = await DocumentPicker.getDocumentAsync({
    type: pdf
      ? ["text/plain", "text/markdown", "application/pdf"]
      : ["text/plain", "text/markdown"],
    multiple: false,
    copyToCacheDirectory: true,
  });
  if (picked.canceled || !picked.assets?.[0]) return null;
  const asset = picked.assets[0];
  const file = new File(asset.uri);
  try {
    if (!file.exists || file.size <= 0 || file.size > 10 * 1024 * 1024)
      throw new Error("Choose a nonempty file of at most 10 MB.");
    const extension = asset.name.split(".").pop()?.toLowerCase();
    const contentType =
      extension === "md" || extension === "markdown"
        ? "text/markdown"
        : extension === "txt"
          ? "text/plain"
          : extension === "pdf" && pdf
            ? "application/pdf"
            : null;
    if (!contentType)
      throw new Error(
        pdf ? "Choose a .txt, .md or .pdf file." : "Choose a .txt or .md file.",
      );
    const bytes = await file.bytes();
    if (!bytes.byteLength || bytes.byteLength > 10 * 1024 * 1024)
      throw new Error("The selected file is empty or exceeds 10 MB.");
    return { name: asset.name, contentType, bytes };
  } finally {
    // The picker copied this file into our private cache; never remove the original.
    if (
      file.uri.startsWith(
        Paths.cache.uri.endsWith("/") ? Paths.cache.uri : `${Paths.cache.uri}/`,
      )
    ) {
      try {
        file.delete();
      } catch {
        /* The OS may already have removed its cache copy. */
      }
    }
  }
}
