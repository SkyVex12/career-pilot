//! Schema constraints that serde does not check: lengths, bounds, consts and formats.
//! The generated `Validate` impls call these.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidationError {
    pub field: &'static str,
    pub message: String,
}

impl fmt::Display for ValidationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.field, self.message)
    }
}

impl std::error::Error for ValidationError {}

pub trait Validate {
    fn validate(&self) -> Result<(), ValidationError>;
}

fn fail(field: &'static str, message: String) -> Result<(), ValidationError> {
    Err(ValidationError { field, message })
}

pub fn min_len(field: &'static str, v: &str, n: usize) -> Result<(), ValidationError> {
    // JSON Schema counts characters, not bytes.
    let len = v.chars().count();
    if len < n { fail(field, format!("must be at least {n} characters, got {len}")) } else { Ok(()) }
}

pub fn max_len(field: &'static str, v: &str, n: usize) -> Result<(), ValidationError> {
    let len = v.chars().count();
    if len > n { fail(field, format!("must be at most {n} characters, got {len}")) } else { Ok(()) }
}

pub fn min_int(field: &'static str, v: u64, n: u64) -> Result<(), ValidationError> {
    if v < n { fail(field, format!("must be at least {n}, got {v}")) } else { Ok(()) }
}

pub fn max_int(field: &'static str, v: u64, n: u64) -> Result<(), ValidationError> {
    if v > n { fail(field, format!("must be at most {n}, got {v}")) } else { Ok(()) }
}

pub fn min_num(field: &'static str, v: f64, n: f64) -> Result<(), ValidationError> {
    if v.is_nan() || v < n { fail(field, format!("must be at least {n}, got {v}")) } else { Ok(()) }
}

pub fn max_num(field: &'static str, v: f64, n: f64) -> Result<(), ValidationError> {
    if v.is_nan() || v > n { fail(field, format!("must be at most {n}, got {v}")) } else { Ok(()) }
}

pub fn const_int(field: &'static str, v: u64, expected: u64) -> Result<(), ValidationError> {
    if v != expected { fail(field, format!("must be {expected}, got {v}")) } else { Ok(()) }
}

pub fn const_str(field: &'static str, v: &str, expected: &str) -> Result<(), ValidationError> {
    if v != expected { fail(field, format!("must be {expected:?}, got {v:?}")) } else { Ok(()) }
}

pub fn one_of(field: &'static str, v: u64, allowed: &[u64]) -> Result<(), ValidationError> {
    if allowed.contains(&v) { Ok(()) } else { fail(field, format!("must be one of {allowed:?}, got {v}")) }
}

/// RFC 3339 `date-time`, as JSON Schema defines the format: a full date, `T`, a time with
/// optional fractional seconds, and a required offset (`Z` or `+hh:mm`).
pub fn date_time(field: &'static str, v: &str) -> Result<(), ValidationError> {
    if is_date_time(v) { Ok(()) } else { fail(field, format!("must be an RFC 3339 date-time, got {v:?}")) }
}

fn is_date_time(v: &str) -> bool {
    let b = v.as_bytes();
    let digits = |r: std::ops::Range<usize>| r.clone().all(|i| b.get(i).is_some_and(u8::is_ascii_digit));
    let num = |r: std::ops::Range<usize>| v[r].parse::<u32>().unwrap_or(u32::MAX);
    if b.len() < 20 || !digits(0..4) || b[4] != b'-' || !digits(5..7) || b[7] != b'-' || !digits(8..10) {
        return false;
    }
    if !matches!(b[10], b'T' | b't') || !digits(11..13) || b[13] != b':' || !digits(14..16) {
        return false;
    }
    if b[16] != b':' || !digits(17..19) {
        return false;
    }
    let (month, day) = (num(5..7), num(8..10));
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return false;
    }
    if num(11..13) > 23 || num(14..16) > 59 || num(17..19) > 60 {
        return false;
    }
    let mut i = 19;
    if b[i] == b'.' {
        i += 1;
        let start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == start {
            return false;
        }
    }
    match &b[i..] {
        [b'Z' | b'z'] => true,
        [b'+' | b'-', ..] if b.len() - i == 6 => {
            digits(i + 1..i + 3)
                && b[i + 3] == b':'
                && digits(i + 4..i + 6)
                && num(i + 1..i + 3) <= 23
                && num(i + 4..i + 6) <= 59
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::is_date_time;

    #[test]
    fn date_times() {
        for ok in [
            "2026-10-05T15:02:11Z",
            "2026-10-05T15:02:11.123456Z",
            "2026-10-05t15:02:11+02:00",
            "2026-10-05T15:02:11.5-05:30",
        ] {
            assert!(is_date_time(ok), "{ok}");
        }
        for bad in [
            "2026-10-05T15:02:11",
            "2026-10-05 15:02:11Z",
            "2026-13-05T15:02:11Z",
            "2026-10-05T15:02:11.Z",
            "2026-10-05T15:02:11+0200",
            "x",
        ] {
            assert!(!is_date_time(bad), "{bad}");
        }
    }
}
