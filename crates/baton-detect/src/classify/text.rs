//! Screen text the way the Python detector sees it, so that the same patterns find the same
//! matches: Python's `str.splitlines`, `str.strip` and `str.rstrip`.

/// Python's `str.isspace` for one character: Unicode `White_Space`, plus the information
/// separators U+001C..U+001F, which Python also treats as whitespace.
#[must_use]
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `str.splitlines()`: every line boundary Python knows, `\r\n` counting once.
#[must_use]
pub fn split_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\u{0b}'
                | '\u{0c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !boundary {
            continue;
        }
        lines.push(&text[start..index]);
        let mut end = index + c.len_utf8();
        if c == '\r' && chars.peek().is_some_and(|&(_, next)| next == '\n') {
            chars.next();
            end += 1;
        }
        start = end;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

/// The last `count` lines that are not blank, right-trimmed and joined by `\n`
/// (the Python detector's `bottom`).
#[must_use]
pub fn bottom(screen: &str, count: usize) -> String {
    let kept: Vec<&str> = split_lines(screen)
        .into_iter()
        .filter(|line| !line.trim_matches(is_space).is_empty())
        .map(|line| line.trim_end_matches(is_space))
        .collect();
    kept[kept.len().saturating_sub(count)..].join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lines_split_where_python_splits_them() {
        assert_eq!(
            split_lines("a\r\nb\rc\x0cd\u{2028}e\n"),
            ["a", "b", "c", "d", "e"]
        );
        assert_eq!(split_lines("a\n\nb"), ["a", "", "b"]);
        assert_eq!(split_lines(""), Vec::<&str>::new());
        assert_eq!(split_lines("\n"), [""]);
    }

    #[test]
    fn bottom_keeps_the_last_non_blank_lines_trimmed_on_the_right() {
        let screen = "one  \n\n \u{1f}\ntwo\t\n  three \n\n";
        assert_eq!(bottom(screen, 2), "two\n  three");
        assert_eq!(bottom(screen, 10), "one\ntwo\n  three");
        assert_eq!(bottom("", 3), "");
    }
}
