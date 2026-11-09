//! The name Task Manager shows for this process.
//!
//! Windows 11 lists the program file's description as the group, and the window title as the
//! row under it. Both are set to the configured name. The description is stored in the program
//! file, so the group shows it the next time the program starts. The file name stays
//! `copilot-desktop.exe`. A signed file is left alone, because rewriting it would break the
//! signature. Names of Windows components are rejected earlier, in `stealth::normalize_display_name`.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// The name written into the program file during this process. Version-info reads are cached
/// for a path, so a second save in the same process must not decide the write failed.
static APPLIED: Mutex<Option<String>> = Mutex::new(None);

/// True when the program file's description is not `name` yet. A failed write leaves this true,
/// so saving the same name again tries the file once more.
pub fn differs(name: &str) -> bool {
    if applied_is(name) {
        return false;
    }
    #[cfg(windows)]
    {
        let Ok(running) = std::env::current_exe() else { return false };
        match description_is(&launch_path(&running), name) {
            Ok(same) => !same,
            Err(_) => true,
        }
    }
    #[cfg(not(windows))]
    {
        let _ = name;
        false
    }
}

/// Write `name` into this program's version resource so the next start shows it in Task Manager.
/// Does nothing when the file already has that description.
pub fn install(name: &str) -> Result<(), String> {
    #[cfg(windows)]
    {
        install_windows(name)
    }
    #[cfg(not(windows))]
    {
        let _ = name;
        Ok(())
    }
}

/// Remove a program file left behind by the previous rename, once this process is not using it.
pub fn cleanup_previous() {
    #[cfg(windows)]
    {
        let Ok(running) = std::env::current_exe() else { return };
        if is_previous(&running) {
            return;
        }
        let _ = std::fs::remove_file(previous_path(&running));
        let _ = std::fs::remove_file(staging_path(&running));
    }
}

#[cfg(windows)]
fn install_windows(name: &str) -> Result<(), String> {
    if name.is_empty() || name.chars().count() > 64 {
        return Err("Enter a short name for this app, such as Copilot.".into());
    }
    if applied_is(name) {
        return Ok(());
    }
    let running = std::env::current_exe().map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let target = launch_path(&running);
    if description_is(&target, name)? {
        mark_applied(name);
        return Ok(());
    }
    if security_directory_size(&target)? > 0 {
        return Err(
            "This copy of the app is signed, so Task Manager keeps the name from the installer.".into()
        );
    }
    let staging = staging_path(&target);
    let _ = std::fs::remove_file(&staging);
    std::fs::copy(&target, &staging).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    if let Err(e) = rewrite_version_file(&staging, name) {
        let _ = std::fs::remove_file(&staging);
        return Err(e);
    }
    // `MoveFile` on the running program returns access denied. A POSIX rename still moves the
    // directory entry, and the mapped image keeps running. If that also fails, a helper swaps
    // the file after this process exits, which is what the next start will launch.
    if let Err(e) = swap_into_place(&staging, &target) {
        if schedule_swap(&staging, &target).is_err() {
            let _ = std::fs::remove_file(&staging);
            return Err(e);
        }
        tracing::warn!("program file will be replaced after exit: {e}");
    }
    mark_applied(name);
    tracing::info!("program description is now \"{name}\"; Task Manager shows it after the next start");
    Ok(())
}

#[cfg(windows)]
fn swap_into_place(staging: &Path, target: &Path) -> Result<(), String> {
    make_writable(target);
    let previous = previous_path(target);
    make_writable(&previous);
    let _ = std::fs::remove_file(&previous);
    rename_replacing(target, &previous)?;
    if let Err(e) = rename_replacing(staging, target) {
        let _ = rename_replacing(&previous, target);
        return Err(e);
    }
    Ok(())
}

/// Rename `from` onto `to` in the same directory. The destination may be the running image.
#[cfg(windows)]
fn rename_replacing(from: &Path, to: &Path) -> Result<(), String> {
    match posix_rename(from, to) {
        Ok(()) => Ok(()),
        Err(_) => std::fs::rename(from, to).map_err(|e| format!("Task Manager could not be updated: {e}")),
    }
}

#[cfg(windows)]
fn posix_rename(from: &Path, to: &Path) -> Result<(), String> {
    // RootDirectory is null, so the new name has to be a full path. A bare file name is read as
    // empty and the POSIX rename deletes the file instead of moving it.
    let wide = wide_path(to);
    if wide.len() > 1024 {
        return Err("Task Manager could not be updated.".into());
    }
    let name_bytes = u32::try_from((wide.len() - 1) * 2).unwrap_or(0);
    // 64-bit: flags, pad, root, length, then the wide path. 32-bit has no pad before the root.
    let header = if cfg!(target_pointer_width = "64") { 20 } else { 12 };
    let mut info = vec![0u8; header + wide.len() * 2];
    let flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS;
    info[0..4].copy_from_slice(&flags.to_le_bytes());
    let len_at = header - 4;
    info[len_at..len_at + 4].copy_from_slice(&name_bytes.to_le_bytes());
    for (index, unit) in wide.iter().enumerate() {
        let at = header + index * 2;
        info[at..at + 2].copy_from_slice(&unit.to_le_bytes());
    }
    let from_wide = wide_path(from);
    let handle = unsafe {
        CreateFileW(
            from_wide.as_ptr(),
            DELETE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            std::ptr::null_mut(),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            std::ptr::null_mut(),
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Err(format!("Task Manager could not be updated ({}).", last_error()));
    }
    let size = u32::try_from(info.len()).unwrap_or(0);
    let ok = unsafe { SetFileInformationByHandle(handle, FILE_RENAME_INFO_EX, info.as_ptr(), size) };
    let err = last_error();
    unsafe { CloseHandle(handle) };
    if ok == 0 {
        return Err(format!("Task Manager could not be updated ({err})."));
    }
    if !to.exists() {
        return Err("Task Manager could not be updated (the program file was not renamed).".into());
    }
    Ok(())
}

#[cfg(windows)]
fn schedule_swap(staging: &Path, target: &Path) -> Result<(), String> {
    let previous = previous_path(target);
    let prev_name = previous.file_name().unwrap_or_default();
    let pid = std::process::id();
    let script = std::env::temp_dir().join(format!("copilot-name-swap-{pid}.ps1"));
    let body = format!(
        "$ErrorActionPreference = 'SilentlyContinue'\n\
         Wait-Process -Id {pid}\n\
         Start-Sleep -Milliseconds 600\n\
         Remove-Item -LiteralPath {prev} -Force\n\
         Rename-Item -LiteralPath {target} -NewName {prev_name}\n\
         Move-Item -LiteralPath {staging} -Destination {target} -Force\n\
         Remove-Item -LiteralPath {prev} -Force\n\
         Remove-Item -LiteralPath {script} -Force\n",
        prev = ps_quote(&previous),
        target = ps_quote(target),
        prev_name = ps_quote(Path::new(prev_name)),
        staging = ps_quote(staging),
        script = ps_quote(&script),
    );
    std::fs::write(&script, body).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    spawn_hidden_powershell(&script)
}

#[cfg(windows)]
fn spawn_hidden_powershell(script: &Path) -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    const NO_WINDOW: u32 = 0x0800_0000;
    const DETACHED: u32 = 0x0000_0008;
    const BREAKAWAY: u32 = 0x0100_0000;
    let mut cmd = std::process::Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-File"]).arg(script);
    cmd.creation_flags(NO_WINDOW | DETACHED | BREAKAWAY);
    if cmd.spawn().is_err() {
        cmd.creation_flags(NO_WINDOW | DETACHED);
        cmd.spawn().map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    }
    Ok(())
}

#[cfg(windows)]
fn ps_quote(path: &Path) -> String {
    format!("'{}'", path.display().to_string().replace('\'', "''"))
}

#[cfg(windows)]
fn make_writable(path: &Path) {
    const READONLY: u32 = 0x1;
    let wide = wide_path(path);
    let attrs = unsafe { GetFileAttributesW(wide.as_ptr()) };
    if attrs != INVALID_FILE_ATTRIBUTES && attrs & READONLY != 0 {
        unsafe { SetFileAttributesW(wide.as_ptr(), attrs & !READONLY) };
    }
}

#[cfg(windows)]
fn description_is(path: &Path, name: &str) -> Result<bool, String> {
    Ok(version_string(&read_version_blob(path)?, "FileDescription")? == name)
}

/// Replace `FileDescription` and `ProductName` in `path` and check that Windows can read them back.
#[cfg(windows)]
pub(crate) fn rewrite_version_file(path: &Path, name: &str) -> Result<(), String> {
    let mut root = parse_root(&read_version_blob(path)?)?;
    if !replace_names(&mut root, name) {
        return Err("The program file has no Task Manager name to change.".into());
    }
    let bytes = serialize(&root)?;
    // tauri-winres writes the version resource as language neutral (0) unless a build sets one.
    let lang = resource_language(path).unwrap_or(0);
    update_version_resource(path, lang, &bytes)?;
    if !image_loads(path) {
        return Err("Task Manager could not be updated: the program file could not be read back.".into());
    }
    if !windows_reads(path, name)? {
        return Err("Task Manager could not read the new name from the program file.".into());
    }
    Ok(())
}

#[cfg(windows)]
fn image_loads(path: &Path) -> bool {
    let wide = wide_path(path);
    let module = unsafe {
        LoadLibraryExW(
            wide.as_ptr(),
            std::ptr::null_mut(),
            LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE,
        )
    };
    if module.is_null() {
        return false;
    }
    unsafe { FreeLibrary(module) };
    true
}

#[cfg(windows)]
fn windows_reads(path: &Path, name: &str) -> Result<bool, String> {
    // GetFileVersionInfo caches by path. Copy to a new path so this check sees the write.
    let check = std::env::temp_dir().join(format!("copilot-name-check-{}.exe", std::process::id()));
    std::fs::copy(path, &check).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let description = read_version_blob(&check).and_then(|blob| version_string(&blob, "FileDescription"));
    let product = read_version_blob(&check).and_then(|blob| version_string(&blob, "ProductName"));
    let _ = std::fs::remove_file(&check);
    Ok(description? == name && product? == name)
}

#[cfg(windows)]
fn is_previous(path: &Path) -> bool {
    path.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(".previous"))
}

#[cfg(windows)]
fn launch_path(running: &Path) -> PathBuf {
    let Some(name) = running.file_name().and_then(|n| n.to_str()) else {
        return running.to_path_buf();
    };
    let Some(stripped) = name.strip_suffix(".previous") else {
        return running.to_path_buf();
    };
    if stripped.is_empty() { running.to_path_buf() } else { running.with_file_name(stripped) }
}

#[cfg(windows)]
fn previous_path(path: &Path) -> PathBuf {
    suffixed(path, ".previous")
}

#[cfg(windows)]
fn staging_path(path: &Path) -> PathBuf {
    suffixed(path, ".name-staging")
}

#[cfg(windows)]
fn suffixed(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(suffix);
    path.with_file_name(name)
}

fn applied_is(name: &str) -> bool {
    APPLIED.lock().ok().is_some_and(|applied| applied.as_deref() == Some(name))
}

fn mark_applied(name: &str) {
    if let Ok(mut applied) = APPLIED.lock() {
        *applied = Some(name.to_string());
    }
}

#[cfg(windows)]
struct Block {
    text: bool,
    key: String,
    value: Vec<u8>,
    children: Vec<Block>,
}

#[cfg(windows)]
fn parse_root(data: &[u8]) -> Result<Block, String> {
    if let Ok((block, _)) = parse_block(data, 0)
        && block.key == "VS_VERSION_INFO"
    {
        return Ok(block);
    }
    let needle = encode_text("VS_VERSION_INFO");
    if let Some(pos) = find_bytes(data, &needle)
        && pos >= 6
        && let Ok((block, _)) = parse_block(data, pos - 6)
        && block.key == "VS_VERSION_INFO"
    {
        return Ok(block);
    }
    Err("The program file has no Task Manager name to change.".into())
}

#[cfg(windows)]
fn parse_block(data: &[u8], offset: usize) -> Result<(Block, usize), String> {
    if offset + 6 > data.len() {
        return Err("The program version information was cut short.".into());
    }
    let length = u16::from_le_bytes([data[offset], data[offset + 1]]) as usize;
    if length < 6 || offset + length > data.len() {
        return Err(format!("The program version information at {offset} is the wrong size."));
    }
    let end = offset + length;
    let value_len = u16::from_le_bytes([data[offset + 2], data[offset + 3]]);
    let text = data[offset + 4] != 0 || data[offset + 5] != 0;
    let mut cursor = offset + 6;
    let key = read_text(data, &mut cursor, end)?;
    cursor = align4(cursor).min(end);
    let nbytes = if text { value_len as usize * 2 } else { value_len as usize };
    if cursor + nbytes > end {
        return Err(format!("The program version value for {key} does not fit."));
    }
    let value = data[cursor..cursor + nbytes].to_vec();
    cursor = align4(cursor + nbytes).min(end);
    let mut children = Vec::new();
    while cursor + 6 <= end {
        let child_len = u16::from_le_bytes([data[cursor], data[cursor + 1]]) as usize;
        if child_len == 0 || cursor + child_len > end {
            break;
        }
        let (child, next) = parse_block(data, cursor)?;
        if next <= cursor {
            break;
        }
        children.push(child);
        cursor = next;
    }
    Ok((Block { text, key, value, children }, end))
}

#[cfg(windows)]
fn serialize(block: &Block) -> Result<Vec<u8>, String> {
    let mut buf = Vec::new();
    write_block(&mut buf, block)?;
    Ok(buf)
}

#[cfg(windows)]
fn write_block(buf: &mut Vec<u8>, block: &Block) -> Result<(), String> {
    let start = buf.len();
    buf.extend_from_slice(&[0, 0, 0, 0, 0, 0]);
    write_text(buf, &block.key);
    pad(buf);
    buf.extend_from_slice(&block.value);
    pad(buf);
    for child in &block.children {
        write_block(buf, child)?;
    }
    pad(buf);
    let total = u16::try_from(buf.len() - start)
        .map_err(|_| "The program version information is too large.".to_string())?;
    let value_len = if block.text { block.value.len() / 2 } else { block.value.len() };
    let value_len =
        u16::try_from(value_len).map_err(|_| "The program version information is too large.".to_string())?;
    buf[start..start + 2].copy_from_slice(&total.to_le_bytes());
    buf[start + 2..start + 4].copy_from_slice(&value_len.to_le_bytes());
    let kind: u16 = if block.text { 1 } else { 0 };
    buf[start + 4..start + 6].copy_from_slice(&kind.to_le_bytes());
    Ok(())
}

#[cfg(windows)]
fn replace_names(block: &mut Block, name: &str) -> bool {
    let mut found = false;
    if block.key == "FileDescription" {
        block.text = true;
        block.value = encode_text(name);
        found = true;
    } else if block.key == "ProductName" {
        block.text = true;
        block.value = encode_text(name);
    }
    for child in &mut block.children {
        if replace_names(child, name) {
            found = true;
        }
    }
    found
}

#[cfg(windows)]
fn version_string(blob: &[u8], key: &str) -> Result<String, String> {
    let mut trans = std::ptr::null_mut();
    let mut trans_len: u32 = 0;
    let trans_key = wide(r"\VarFileInfo\Translation");
    let ok = unsafe { VerQueryValueW(blob.as_ptr(), trans_key.as_ptr(), &mut trans, &mut trans_len) };
    if ok == 0 || trans.is_null() || trans_len < 4 {
        return Err("The program version information has no language.".into());
    }
    let words = unsafe { std::slice::from_raw_parts(trans, 4) };
    let lang = u16::from_le_bytes([words[0], words[1]]);
    let codepage = u16::from_le_bytes([words[2], words[3]]);
    let query = wide(&format!("\\StringFileInfo\\{lang:04X}{codepage:04X}\\{key}"));
    let mut ptr = std::ptr::null_mut();
    let mut len: u32 = 0;
    let ok = unsafe { VerQueryValueW(blob.as_ptr(), query.as_ptr(), &mut ptr, &mut len) };
    if ok == 0 || ptr.is_null() || len == 0 {
        return Err(format!("The program file has no {key}."));
    }
    // For a string, the reported length is a count of UTF-16 code units, including the null.
    // Treating it as a byte count cuts the name in half.
    let count = (len as usize).min(1024);
    let bytes = unsafe { std::slice::from_raw_parts(ptr, count.saturating_mul(2)) };
    Ok(decode_text(bytes))
}

#[cfg(windows)]
fn read_version_blob(path: &Path) -> Result<Vec<u8>, String> {
    let wide = wide_path(path);
    let size = unsafe { GetFileVersionInfoSizeW(wide.as_ptr(), std::ptr::null_mut()) };
    if size == 0 {
        return Err(format!("The program file has no Task Manager name ({}).", last_error()));
    }
    let mut buf = vec![0u8; size as usize];
    let ok = unsafe { GetFileVersionInfoW(wide.as_ptr(), 0, size, buf.as_mut_ptr()) };
    if ok == 0 {
        return Err(format!("The program file has no Task Manager name ({}).", last_error()));
    }
    Ok(buf)
}

#[cfg(windows)]
fn resource_language(path: &Path) -> Option<u16> {
    let wide = wide_path(path);
    let module = unsafe {
        LoadLibraryExW(
            wide.as_ptr(),
            std::ptr::null_mut(),
            LOAD_LIBRARY_AS_DATAFILE | LOAD_LIBRARY_AS_IMAGE_RESOURCE,
        )
    };
    if module.is_null() {
        return None;
    }
    let mut lang = u16::MAX;
    let ok = unsafe {
        EnumResourceLanguagesW(
            module,
            make_int_resource(RT_VERSION),
            make_int_resource(1),
            Some(first_language),
            (&mut lang as *mut u16).cast::<std::ffi::c_void>() as isize,
        )
    };
    unsafe { FreeLibrary(module) };
    if ok == 0 || lang == u16::MAX { None } else { Some(lang) }
}

#[cfg(windows)]
unsafe extern "system" fn first_language(
    _module: *mut std::ffi::c_void,
    _kind: *const u16,
    _name: *const u16,
    language: u16,
    param: isize,
) -> i32 {
    let slot = param as *mut u16;
    if !slot.is_null() {
        unsafe { *slot = language }
    }
    0
}

#[cfg(windows)]
fn update_version_resource(path: &Path, lang: u16, bytes: &[u8]) -> Result<(), String> {
    let wide = wide_path(path);
    let size = u32::try_from(bytes.len())
        .map_err(|_| "The program version information is too large.".to_string())?;
    let handle = unsafe { BeginUpdateResourceW(wide.as_ptr(), 0) };
    if handle.is_null() {
        return Err(format!("Task Manager could not be updated ({}).", last_error()));
    }
    let ok = unsafe {
        UpdateResourceW(
            handle,
            make_int_resource(RT_VERSION),
            make_int_resource(1),
            lang,
            bytes.as_ptr(),
            size,
        )
    };
    if ok == 0 {
        let err = last_error();
        unsafe { EndUpdateResourceW(handle, 1) };
        return Err(format!("Task Manager could not be updated ({err})."));
    }
    if unsafe { EndUpdateResourceW(handle, 0) } == 0 {
        return Err(format!("Task Manager could not be updated ({}).", last_error()));
    }
    Ok(())
}

#[cfg(windows)]
fn security_directory_size(path: &Path) -> Result<u32, String> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file =
        std::fs::File::open(path).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let mut dos = [0u8; 64];
    file.read_exact(&mut dos).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    if &dos[0..2] != b"MZ" {
        return Err("Task Manager could not be updated: the program file is not an executable.".into());
    }
    let lfanew = u32::from_le_bytes([dos[60], dos[61], dos[62], dos[63]]) as u64;
    file.seek(SeekFrom::Start(lfanew)).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let mut sig = [0u8; 4];
    file.read_exact(&mut sig).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    if &sig != b"PE\0\0" {
        return Err("Task Manager could not be updated: the program file is not an executable.".into());
    }
    let mut coff = [0u8; 20];
    file.read_exact(&mut coff).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let opt_size = u16::from_le_bytes([coff[16], coff[17]]) as u64;
    let opt_pos = file.stream_position().map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let mut magic = [0u8; 2];
    file.read_exact(&mut magic).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let directories = match u16::from_le_bytes(magic) {
        0x20b => 112u64,
        0x10b => 96u64,
        _ => return Err("Task Manager could not be updated: the program file has an unknown format.".into()),
    };
    // IMAGE_DIRECTORY_ENTRY_SECURITY is directory 4. No entry means the file is unsigned.
    if opt_size < directories + 8 * 5 {
        return Ok(0);
    }
    file.seek(SeekFrom::Start(opt_pos + directories + 4 * 8))
        .map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    let mut entry = [0u8; 8];
    file.read_exact(&mut entry).map_err(|e| format!("Task Manager could not be updated: {e}"))?;
    Ok(u32::from_le_bytes([entry[4], entry[5], entry[6], entry[7]]))
}

#[cfg(windows)]
fn align4(n: usize) -> usize {
    n.div_ceil(4) * 4
}

#[cfg(windows)]
fn pad(buf: &mut Vec<u8>) {
    while !buf.len().is_multiple_of(4) {
        buf.push(0);
    }
}

#[cfg(windows)]
fn encode_text(text: &str) -> Vec<u8> {
    let mut bytes = Vec::new();
    write_text(&mut bytes, text);
    bytes
}

#[cfg(windows)]
fn write_text(buf: &mut Vec<u8>, text: &str) {
    for unit in text.encode_utf16() {
        buf.extend(unit.to_le_bytes());
    }
    buf.extend(0u16.to_le_bytes());
}

#[cfg(windows)]
fn read_text(data: &[u8], cursor: &mut usize, end: usize) -> Result<String, String> {
    let start = *cursor;
    while *cursor + 1 < end {
        let unit = u16::from_le_bytes([data[*cursor], data[*cursor + 1]]);
        *cursor += 2;
        if unit == 0 {
            return Ok(decode_text(&data[start..*cursor]));
        }
    }
    Err("The program version information was cut short.".into())
}

#[cfg(windows)]
fn decode_text(bytes: &[u8]) -> String {
    let (chunks, _) = bytes.as_chunks::<2>();
    let units: Vec<u16> = chunks.iter().map(|c| u16::from_le_bytes(*c)).collect();
    let end = units.iter().position(|unit| *unit == 0).unwrap_or(units.len());
    String::from_utf16_lossy(&units[..end])
}

#[cfg(windows)]
fn find_bytes(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|window| window == needle)
}

#[cfg(windows)]
fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

#[cfg(windows)]
fn wide_path(path: &Path) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;
    path.as_os_str().encode_wide().chain(std::iter::once(0)).collect()
}

#[cfg(windows)]
fn make_int_resource(id: u16) -> *const u16 {
    id as usize as *const u16
}

#[cfg(windows)]
fn last_error() -> u32 {
    unsafe { GetLastError() }
}

#[cfg(windows)]
const FILE_RENAME_FLAG_REPLACE_IF_EXISTS: u32 = 0x1;
#[cfg(windows)]
const FILE_RENAME_FLAG_POSIX_SEMANTICS: u32 = 0x2;
#[cfg(windows)]
const FILE_RENAME_INFO_EX: i32 = 22;
#[cfg(windows)]
const DELETE: u32 = 0x0001_0000;
#[cfg(windows)]
const FILE_SHARE_READ: u32 = 0x1;
#[cfg(windows)]
const FILE_SHARE_WRITE: u32 = 0x2;
#[cfg(windows)]
const FILE_SHARE_DELETE: u32 = 0x4;
#[cfg(windows)]
const OPEN_EXISTING: u32 = 3;
#[cfg(windows)]
const FILE_ATTRIBUTE_NORMAL: u32 = 0x80;
#[cfg(windows)]
const INVALID_HANDLE_VALUE: *mut std::ffi::c_void = -1isize as *mut std::ffi::c_void;
#[cfg(windows)]
const INVALID_FILE_ATTRIBUTES: u32 = 0xFFFF_FFFF;

#[cfg(windows)]
const RT_VERSION: u16 = 16;
#[cfg(windows)]
const LOAD_LIBRARY_AS_DATAFILE: u32 = 0x2;
#[cfg(windows)]
const LOAD_LIBRARY_AS_IMAGE_RESOURCE: u32 = 0x20;

#[cfg(windows)]
#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetLastError() -> u32;
    fn CreateFileW(
        name: *const u16,
        access: u32,
        share: u32,
        security: *mut std::ffi::c_void,
        disposition: u32,
        flags: u32,
        template: *mut std::ffi::c_void,
    ) -> *mut std::ffi::c_void;
    fn SetFileInformationByHandle(file: *mut std::ffi::c_void, class: i32, info: *const u8, size: u32)
    -> i32;
    fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
    fn GetFileAttributesW(name: *const u16) -> u32;
    fn SetFileAttributesW(name: *const u16, attrs: u32) -> i32;
    fn LoadLibraryExW(name: *const u16, file: *mut std::ffi::c_void, flags: u32) -> *mut std::ffi::c_void;
    fn FreeLibrary(module: *mut std::ffi::c_void) -> i32;
    fn EnumResourceLanguagesW(
        module: *mut std::ffi::c_void,
        kind: *const u16,
        name: *const u16,
        callback: Option<
            unsafe extern "system" fn(*mut std::ffi::c_void, *const u16, *const u16, u16, isize) -> i32,
        >,
        param: isize,
    ) -> i32;
    fn BeginUpdateResourceW(file: *const u16, delete_existing: i32) -> *mut std::ffi::c_void;
    fn UpdateResourceW(
        update: *mut std::ffi::c_void,
        kind: *const u16,
        name: *const u16,
        language: u16,
        data: *const u8,
        size: u32,
    ) -> i32;
    fn EndUpdateResourceW(update: *mut std::ffi::c_void, discard: i32) -> i32;
}

#[cfg(windows)]
#[link(name = "version")]
unsafe extern "system" {
    fn GetFileVersionInfoSizeW(name: *const u16, handle: *mut u32) -> u32;
    fn GetFileVersionInfoW(name: *const u16, handle: u32, len: u32, data: *mut u8) -> i32;
    fn VerQueryValueW(block: *const u8, sub: *const u16, buffer: *mut *mut u8, len: *mut u32) -> i32;
}

#[cfg(all(test, windows))]
mod tests {
    #[test]
    fn a_longer_name_round_trips_through_the_version_block() {
        // The built-in description is "Copilot Desktop" (15 characters). A longer name has to
        // rebuild the version block; overwriting the old characters in place would not fit.
        let name = "Interview Copilot Notes";
        let bytes = super::serialize(&version_tree(name)).expect("serialize");
        let root = super::parse_root(&bytes).expect("parse");
        assert_eq!(text(&root, "FileDescription").as_deref(), Some(name));
        assert_eq!(text(&root, "ProductName").as_deref(), Some(name));
        assert_eq!(text(&root, "OriginalFilename").as_deref(), Some("copilot-desktop.exe"));
    }

    #[test]
    fn windows_reads_a_name_longer_than_the_built_in_description() {
        let name = "Interview Copilot Notes";
        let bytes = super::serialize(&version_tree(name)).expect("serialize");
        assert_eq!(super::version_string(&bytes, "FileDescription").expect("description"), name);
        assert_eq!(super::version_string(&bytes, "ProductName").expect("product"), name);
        assert_eq!(
            super::version_string(&bytes, "OriginalFilename").expect("filename"),
            "copilot-desktop.exe"
        );
    }

    #[test]
    fn rewriting_the_desktop_executable_changes_the_name_task_manager_reads() {
        let Some(src) = desktop_exe() else { return };
        assert_eq!(super::security_directory_size(&src).expect("signature"), 0, "the dev build is unsigned");
        let tmp = std::env::temp_dir().join(format!("copilot-name-rewrite-{}.exe", std::process::id()));
        std::fs::copy(&src, &tmp).expect("copy");
        let result = super::rewrite_version_file(&tmp, "Interview Copilot Notes");
        let _ = std::fs::remove_file(&tmp);
        result.expect("rewrite");
    }

    fn version_tree(description: &str) -> super::Block {
        super::Block {
            text: false,
            key: "VS_VERSION_INFO".into(),
            value: fixed_info(),
            children: vec![
                super::Block {
                    text: true,
                    key: "StringFileInfo".into(),
                    value: Vec::new(),
                    children: vec![super::Block {
                        text: true,
                        key: "040904B0".into(),
                        value: Vec::new(),
                        children: vec![
                            string_block("CompanyName", "Copilot"),
                            string_block("FileDescription", description),
                            string_block("FileVersion", "0.0.0"),
                            string_block("InternalName", "copilot-desktop"),
                            string_block("OriginalFilename", "copilot-desktop.exe"),
                            string_block("ProductName", description),
                            string_block("ProductVersion", "0.0.0"),
                        ],
                    }],
                },
                super::Block {
                    text: true,
                    key: "VarFileInfo".into(),
                    value: Vec::new(),
                    children: vec![super::Block {
                        text: false,
                        key: "Translation".into(),
                        value: {
                            let mut value = Vec::new();
                            value.extend(0x0409u16.to_le_bytes());
                            value.extend(0x04B0u16.to_le_bytes());
                            value
                        },
                        children: Vec::new(),
                    }],
                },
            ],
        }
    }

    fn string_block(key: &str, value: &str) -> super::Block {
        super::Block { text: true, key: key.into(), value: super::encode_text(value), children: Vec::new() }
    }

    fn fixed_info() -> Vec<u8> {
        let mut value = vec![0u8; 52];
        value[0..4].copy_from_slice(&0xFEEF_04BDu32.to_le_bytes());
        value[4..8].copy_from_slice(&0x0001_0000u32.to_le_bytes());
        value
    }

    fn text(block: &super::Block, key: &str) -> Option<String> {
        if block.key == key && block.text {
            return Some(super::decode_text(&block.value));
        }
        block.children.iter().find_map(|child| text(child, key))
    }

    fn desktop_exe() -> Option<std::path::PathBuf> {
        let exe = std::env::current_exe().ok()?;
        let dir = exe.parent()?.parent()?;
        let candidate = dir.join("copilot-desktop.exe");
        candidate.is_file().then_some(candidate)
    }

    #[test]
    fn a_running_programs_stand_in_can_be_renamed_with_posix_semantics() {
        let dir = std::env::temp_dir().join(format!("copilot-rename-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("dir");
        let from = dir.join("copilot-desktop.exe");
        let to = dir.join("copilot-desktop.exe.previous");
        std::fs::write(&from, b"not a real program").expect("write");
        super::posix_rename(&from, &to).expect("posix rename");
        assert!(!from.exists());
        assert_eq!(std::fs::read(&to).expect("read"), b"not a real program");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
