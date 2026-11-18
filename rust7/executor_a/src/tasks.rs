//! Задачи шага 6: генератор SplitMix64, хранилище задач, верные ответы и
//! стоимость проверки. Всё детерминировано зерном и номером контура.

use std::collections::BTreeMap;

const WORDS: [&str; 20] = [
    "река", "мост", "город", "свет", "ветер", "дорога", "камень", "окно", "берег", "поле", "лес", "гора", "море",
    "небо", "дом", "сад", "путь", "огонь", "снег", "трава",
];

pub struct SplitMix(pub u64);

impl SplitMix {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

#[derive(Clone, Debug)]
pub struct TaskParams {
    pub seed: i64,
    pub sorts: u32,
    pub searches: u32,
    pub tables: u32,
    pub docs: u32,
    pub table_rows: u32,
    pub table_cols: u32,
    pub doc_sections: u32,
    pub doc_words: u32,
    pub parts_table: u32,
    pub parts_doc: u32,
}

#[derive(Clone, Debug)]
pub enum Rec {
    Sort(Vec<i32>),
    Search(i32, Vec<i32>),
    Table { rows: u32, cols: u32, cells: Vec<i32> },
    Doc { a: Vec<u8>, b: Vec<u8>, text: Vec<u8> },
}

#[derive(Clone, Debug)]
pub struct Part {
    pub task: u8,
    pub kind: u8,
    pub part: u8,
    pub parts: u8,
    pub rec: Rec,
}

pub struct Store {
    pub parts: Vec<Part>,
    pub bytes: Vec<u8>,
    pub index: BTreeMap<(u8, u8), usize>,
}

fn i32s(v: &[i32], out: &mut Vec<u8>) {
    for x in v {
        out.extend_from_slice(&x.to_le_bytes());
    }
}

fn data_of(r: &Rec) -> Vec<u8> {
    let mut d = Vec::new();
    match r {
        Rec::Sort(xs) => {
            d.extend_from_slice(&(xs.len() as u32).to_le_bytes());
            i32s(xs, &mut d);
        }
        Rec::Search(t, xs) => {
            d.extend_from_slice(&t.to_le_bytes());
            d.extend_from_slice(&(xs.len() as u32).to_le_bytes());
            i32s(xs, &mut d);
        }
        Rec::Table { rows, cols, cells } => {
            d.extend_from_slice(&rows.to_le_bytes());
            d.extend_from_slice(&cols.to_le_bytes());
            i32s(cells, &mut d);
        }
        Rec::Doc { a, b, text } => {
            d.push(a.len() as u8);
            d.extend_from_slice(a);
            d.push(b.len() as u8);
            d.extend_from_slice(b);
            d.extend_from_slice(&(text.len() as u32).to_le_bytes());
            d.extend_from_slice(text);
        }
    }
    d
}

pub fn generate(p: &TaskParams, contour: usize) -> Store {
    let mut g = SplitMix((p.seed as u64).wrapping_mul(1000).wrapping_add(contour as u64));
    let mut parts: Vec<Part> = Vec::new();
    let mut task: u8 = 0;
    for _ in 0..p.sorts {
        task += 1;
        let xs: Vec<i32> = (0..64).map(|_| g.below(1000) as i32).collect();
        parts.push(Part { task, kind: 1, part: 0, parts: 1, rec: Rec::Sort(xs) });
    }
    for _ in 0..p.searches {
        task += 1;
        let xs: Vec<i32> = (0..64).map(|_| g.below(16) as i32).collect();
        let t = if g.below(10) < 8 { g.below(16) as i32 } else { 16 + g.below(4) as i32 };
        parts.push(Part { task, kind: 2, part: 0, parts: 1, rec: Rec::Search(t, xs) });
    }
    for _ in 0..p.tables {
        task += 1;
        let (r, c) = (p.table_rows as usize, p.table_cols as usize);
        let cells: Vec<i32> = (0..r * c).map(|_| g.below(100) as i32).collect();
        let k = p.parts_table as usize;
        for q in 0..k {
            let (lo, hi) = (q * r / k, (q + 1) * r / k);
            parts.push(Part {
                task,
                kind: 3,
                part: q as u8,
                parts: k as u8,
                rec: Rec::Table { rows: (hi - lo) as u32, cols: c as u32, cells: cells[lo * c..hi * c].to_vec() },
            });
        }
    }
    for _ in 0..p.docs {
        task += 1;
        let a = g.below(20) as usize;
        let mut b = g.below(19) as usize;
        if b >= a {
            b += 1;
        }
        let mut sections: Vec<String> = Vec::new();
        for _ in 0..p.doc_sections {
            let mut w: Vec<&str> = (0..p.doc_words).map(|_| WORDS[g.below(20) as usize]).collect();
            let at = g.below(p.doc_words as u64) as usize;
            w[at] = WORDS[a];
            let line = w.join(" ");
            let mut ch = line.chars();
            let first: String = ch.next().map(|c| c.to_uppercase().collect()).unwrap_or_default();
            sections.push(format!("{}{}.", first, ch.as_str()));
        }
        let s = p.doc_sections as usize;
        let k = p.parts_doc as usize;
        for q in 0..k {
            let (lo, hi) = (q * s / k, (q + 1) * s / k);
            parts.push(Part {
                task,
                kind: 4,
                part: q as u8,
                parts: k as u8,
                rec: Rec::Doc {
                    a: WORDS[a].as_bytes().to_vec(),
                    b: WORDS[b].as_bytes().to_vec(),
                    text: sections[lo..hi].join("\n\n").into_bytes(),
                },
            });
        }
    }
    let mut bytes = vec![parts.len() as u8];
    let mut index = BTreeMap::new();
    for (n, pt) in parts.iter().enumerate() {
        let d = data_of(&pt.rec);
        bytes.extend_from_slice(&[pt.task, pt.kind, pt.part, pt.parts]);
        bytes.extend_from_slice(&(d.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&d);
        index.insert((pt.task, pt.part), n);
    }
    Store { parts, bytes, index }
}

fn le_i32(a: &[u8]) -> Option<i32> {
    if a.len() == 4 {
        Some(i32::from_le_bytes([a[0], a[1], a[2], a[3]]))
    } else {
        None
    }
}

/// Верен ли ответ. `canonical`: у поиска верно только первое вхождение.
pub fn is_correct(rec: &Rec, answer: &[u8], canonical: bool) -> bool {
    match rec {
        Rec::Sort(xs) => {
            let mut s = xs.clone();
            s.sort();
            let mut want = Vec::new();
            i32s(&s, &mut want);
            answer == &want[..]
        }
        Rec::Search(t, xs) => {
            let Some(v) = le_i32(answer) else { return false };
            let hits: Vec<usize> = xs.iter().enumerate().filter(|(_, x)| *x == t).map(|(k, _)| k).collect();
            if hits.is_empty() {
                v == -1
            } else if canonical {
                v == hits[0] as i32
            } else {
                v >= 0 && hits.contains(&(v as usize))
            }
        }
        Rec::Table { rows, cols, cells } => {
            let (r, c) = (*rows as usize, *cols as usize);
            let mut sums = vec![0i32; c];
            for q in 0..r {
                for j in 0..c {
                    sums[j] = sums[j].wrapping_add(cells[q * c + j]);
                }
            }
            let total = sums.iter().fold(0i32, |a, b| a.wrapping_add(*b));
            sums.push(total);
            let mut want = Vec::new();
            i32s(&sums, &mut want);
            answer == &want[..]
        }
        Rec::Doc { a, b, text } => {
            let mut out = Vec::new();
            let mut i = 0;
            while i < text.len() {
                if !a.is_empty() && text[i..].starts_with(a) {
                    out.extend_from_slice(b);
                    i += a.len();
                } else {
                    out.push(text[i]);
                    i += 1;
                }
            }
            answer == &out[..]
        }
    }
}

/// Стоимость проверки v = max(1, ⌈w / единица⌉); w — объём проверки.
pub fn check_cost(rec: &Rec, answer: &[u8], unit: i64) -> i64 {
    let w: i64 = match rec {
        Rec::Sort(xs) => xs.len() as i64,
        Rec::Search(_, xs) => {
            if le_i32(answer) == Some(-1) {
                xs.len() as i64
            } else {
                1
            }
        }
        Rec::Table { rows, cols, .. } => *rows as i64 * *cols as i64,
        Rec::Doc { text, .. } => text.len() as i64,
    };
    let unit = unit.max(1);
    std::cmp::max(1, (w + unit - 1) / unit)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canon::{hex, sha_hex};

    fn defaults(seed: i64) -> TaskParams {
        TaskParams {
            seed, sorts: 1, searches: 2, tables: 1, docs: 1, table_rows: 8, table_cols: 6, doc_sections: 8,
            doc_words: 10, parts_table: 1, parts_doc: 1,
        }
    }

    #[test]
    fn splitmix() {
        let mut g = SplitMix(1001);
        let got: Vec<u64> = (0..5).map(|_| g.next()).collect();
        assert_eq!(got, vec![5998232818650842836, 4234890295383215704, 4212179000090341297, 8927971578246012719, 9352498596323579307]);
    }

    #[test]
    fn stores() {
        let s = generate(&defaults(0), 1);
        assert_eq!(s.bytes.len(), 1847);
        assert_eq!(sha_hex(&s.bytes), "44be439a42eaae488c6b65fd6021539c061765e6696da194909f1fef22b8ddeb");
        assert_eq!(hex(&s.bytes[..48]), "05010100010401000040000000d1010000070200004e020000eb000000f9020000300000002d00000015020000080200");
        let s = generate(&defaults(0), 2);
        assert_eq!((s.bytes.len(), sha_hex(&s.bytes)), (1827, "af784cc868f9ee450e3f0ae392f26c7bc68dd96d8d6d1315639c28178c267adf".to_string()));
        let s = generate(&defaults(7), 1);
        assert_eq!((s.bytes.len(), sha_hex(&s.bytes)), (1841, "72d5322649fe4e3abbd90ea3c7814f41b81a2806dcc33488d5298b51aeeacc2d".to_string()));
        let s = generate(&defaults(7), 2);
        assert_eq!((s.bytes.len(), sha_hex(&s.bytes)), (1833, "d402c5f4f7b68653142bf12ea05aa1793a059b5b1bcb6338ca2d59100ccfbe73".to_string()));
        let mut p = defaults(3);
        p.parts_table = 4;
        p.parts_doc = 2;
        p.table_rows = 16;
        let s = generate(&p, 1);
        assert_eq!((s.bytes.len(), sha_hex(&s.bytes)), (2099, "c5e8716f700b19073387bf0c22c0ef86839d44ebe1f091cd9c4dbeba95c9a671".to_string()));
    }
}
