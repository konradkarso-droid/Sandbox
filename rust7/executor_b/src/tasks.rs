//! Задачи шага 6: генератор SplitMix64, хранилище задач, верный ответ и
//! объём проверки (контракт, §11).

use std::collections::BTreeMap;

const WORDS: [&str; 20] = [
    "река", "мост", "город", "свет", "ветер", "дорога", "камень", "окно", "берег", "поле", "лес", "гора",
    "море", "небо", "дом", "сад", "путь", "огонь", "снег", "трава",
];

pub struct SplitMix(pub u64);

impl SplitMix {
    pub fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    pub fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

/// Размеры и зерно задач — поля настроек шага 6.
pub struct TaskParams {
    pub task_seed: i64,
    pub sorts: i64,
    pub searches: i64,
    pub tables: i64,
    pub docs: i64,
    pub table_rows: i64,
    pub table_cols: i64,
    pub doc_sections: i64,
    pub doc_words: i64,
    pub parts_table: i64,
    pub parts_doc: i64,
}

/// Одна часть задачи: запись хранилища.
pub struct Part {
    pub kind: u8,
    pub data: Vec<u8>,
}

pub struct TaskSet {
    /// Все части по (задача, часть).
    pub parts: BTreeMap<(i64, i64), Part>,
    /// Байты хранилища — данные объекта `задания{i}`.
    pub store: Vec<u8>,
}

fn put_i32(d: &mut Vec<u8>, x: i32) {
    d.extend_from_slice(&x.to_le_bytes());
}

fn capitalize(s: &str) -> String {
    let mut it = s.chars();
    match it.next() {
        Some(c) => c.to_uppercase().collect::<String>() + it.as_str(),
        None => String::new(),
    }
}

pub fn generate(p: &TaskParams, contour: i64) -> TaskSet {
    let mut g = SplitMix((p.task_seed as u64).wrapping_mul(1000).wrapping_add(contour as u64));
    let mut recs: Vec<(u8, u8, u8, u8, Vec<u8>)> = Vec::new(); // задача, вид, часть, частей, данные
    let mut task: u8 = 0;
    for _ in 0..p.sorts {
        task += 1;
        let xs: Vec<i32> = (0..64).map(|_| g.below(1000) as i32).collect();
        let mut d = Vec::new();
        put_i32(&mut d, xs.len() as i32);
        xs.iter().for_each(|x| put_i32(&mut d, *x));
        recs.push((task, 1, 0, 1, d));
    }
    for _ in 0..p.searches {
        task += 1;
        let xs: Vec<i32> = (0..64).map(|_| g.below(16) as i32).collect();
        let target = if g.below(10) < 8 { g.below(16) as i32 } else { 16 + g.below(4) as i32 };
        let mut d = Vec::new();
        put_i32(&mut d, target);
        put_i32(&mut d, xs.len() as i32);
        xs.iter().for_each(|x| put_i32(&mut d, *x));
        recs.push((task, 2, 0, 1, d));
    }
    for _ in 0..p.tables {
        task += 1;
        let (r, c) = (p.table_rows, p.table_cols);
        let cells: Vec<i32> = (0..r * c).map(|_| g.below(100) as i32).collect();
        let k = p.parts_table;
        for part in 0..k {
            let (lo, hi) = (part * r / k, (part + 1) * r / k);
            let mut d = Vec::new();
            put_i32(&mut d, (hi - lo) as i32);
            put_i32(&mut d, c as i32);
            for x in &cells[(lo * c) as usize..(hi * c) as usize] {
                put_i32(&mut d, *x);
            }
            recs.push((task, 3, part as u8, k as u8, d));
        }
    }
    for _ in 0..p.docs {
        task += 1;
        let a = g.below(20) as usize;
        let mut b = g.below(19) as usize;
        if b >= a {
            b += 1;
        }
        let (wa, wb) = (WORDS[a], WORDS[b]);
        let mut sections = Vec::new();
        for _ in 0..p.doc_sections {
            let mut ws: Vec<&str> = (0..p.doc_words).map(|_| WORDS[g.below(20) as usize]).collect();
            let at = g.below(p.doc_words as u64) as usize;
            ws[at] = wa;
            sections.push(capitalize(&ws.join(" ")) + ".");
        }
        let (s, k) = (p.doc_sections, p.parts_doc);
        for part in 0..k {
            let (lo, hi) = (part * s / k, (part + 1) * s / k);
            let text = sections[lo as usize..hi as usize].join("\n\n");
            let mut d = vec![wa.len() as u8];
            d.extend_from_slice(wa.as_bytes());
            d.push(wb.len() as u8);
            d.extend_from_slice(wb.as_bytes());
            d.extend_from_slice(&(text.len() as u32).to_le_bytes());
            d.extend_from_slice(text.as_bytes());
            recs.push((task, 4, part as u8, k as u8, d));
        }
    }
    let mut store = vec![recs.len() as u8];
    let mut parts = BTreeMap::new();
    for (t, kind, part, parts_n, d) in recs {
        store.extend_from_slice(&[t, kind, part, parts_n]);
        store.extend_from_slice(&(d.len() as u32).to_le_bytes());
        store.extend_from_slice(&d);
        parts.insert((t as i64, part as i64), Part { kind, data: d });
    }
    TaskSet { parts, store }
}

fn rd32(d: &[u8], at: usize) -> i32 {
    i32::from_le_bytes([d[at], d[at + 1], d[at + 2], d[at + 3]])
}

/// Проверка ответа: (верен ли, объём проверки w). Иная длина — неверно.
pub fn check(part: &Part, ans: &[u8], canonical: bool) -> (bool, i64) {
    let d = &part.data;
    match part.kind {
        1 => {
            let n = rd32(d, 0) as usize;
            let mut xs: Vec<i32> = (0..n).map(|k| rd32(d, 4 + 4 * k)).collect();
            xs.sort();
            let want: Vec<u8> = xs.iter().flat_map(|x| x.to_le_bytes()).collect();
            (ans == want.as_slice(), n as i64)
        }
        2 => {
            let target = rd32(d, 0);
            let n = rd32(d, 4) as usize;
            let xs: Vec<i32> = (0..n).map(|k| rd32(d, 8 + 4 * k)).collect();
            let hits: Vec<usize> = (0..n).filter(|k| xs[*k] == target).collect();
            let w = if ans.len() == 4 && rd32(ans, 0) == -1 { n as i64 } else { 1 };
            if ans.len() != 4 {
                return (false, w);
            }
            let got = rd32(ans, 0);
            let ok = if hits.is_empty() {
                got == -1
            } else if canonical {
                got == hits[0] as i32
            } else {
                got >= 0 && hits.contains(&(got as usize))
            };
            (ok, w)
        }
        3 => {
            let (r, c) = (rd32(d, 0) as usize, rd32(d, 4) as usize);
            let mut sums = vec![0i32; c];
            for q in 0..r {
                for (j, s) in sums.iter_mut().enumerate() {
                    *s = s.wrapping_add(rd32(d, 8 + 4 * (q * c + j)));
                }
            }
            let total = sums.iter().fold(0i32, |a, b| a.wrapping_add(*b));
            sums.push(total);
            let want: Vec<u8> = sums.iter().flat_map(|x| x.to_le_bytes()).collect();
            (ans == want.as_slice(), (r * c) as i64)
        }
        _ => {
            let la = d[0] as usize;
            let a = &d[1..1 + la];
            let lb = d[1 + la] as usize;
            let b = &d[2 + la..2 + la + lb];
            let tl = u32::from_le_bytes([d[2 + la + lb], d[3 + la + lb], d[4 + la + lb], d[5 + la + lb]]) as usize;
            let t = &d[6 + la + lb..6 + la + lb + tl];
            let mut out = Vec::new();
            let mut i = 0;
            while i < t.len() {
                if !a.is_empty() && t[i..].starts_with(a) {
                    out.extend_from_slice(b);
                    i += a.len();
                } else {
                    out.push(t[i]);
                    i += 1;
                }
            }
            (ans == out.as_slice(), tl as i64)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::hex;
    use sha2::{Digest, Sha256};

    fn params(seed: i64) -> TaskParams {
        TaskParams {
            task_seed: seed, sorts: 1, searches: 2, tables: 1, docs: 1, table_rows: 8, table_cols: 6,
            doc_sections: 8, doc_words: 10, parts_table: 1, parts_doc: 1,
        }
    }

    #[test]
    fn splitmix_vektor() {
        let mut g = SplitMix(1001);
        let got: Vec<u64> = (0..5).map(|_| g.next()).collect();
        assert_eq!(
            got,
            vec![5998232818650842836, 4234890295383215704, 4212179000090341297, 8927971578246012719, 9352498596323579307]
        );
    }

    fn sha(t: &TaskSet) -> (usize, String) {
        (t.store.len(), hex(&Sha256::digest(&t.store)))
    }

    #[test]
    fn hranilishche_vektory() {
        assert_eq!(sha(&generate(&params(0), 1)),
            (1847, "44be439a42eaae488c6b65fd6021539c061765e6696da194909f1fef22b8ddeb".into()));
        assert_eq!(sha(&generate(&params(0), 2)),
            (1827, "af784cc868f9ee450e3f0ae392f26c7bc68dd96d8d6d1315639c28178c267adf".into()));
        assert_eq!(sha(&generate(&params(7), 1)),
            (1841, "72d5322649fe4e3abbd90ea3c7814f41b81a2806dcc33488d5298b51aeeacc2d".into()));
        assert_eq!(sha(&generate(&params(7), 2)),
            (1833, "d402c5f4f7b68653142bf12ea05aa1793a059b5b1bcb6338ca2d59100ccfbe73".into()));
        let mut p = params(3);
        p.parts_table = 4;
        p.parts_doc = 2;
        p.table_rows = 16;
        assert_eq!(sha(&generate(&p, 1)),
            (2099, "c5e8716f700b19073387bf0c22c0ef86839d44ebe1f091cd9c4dbeba95c9a671".into()));
        assert_eq!(hex(&generate(&params(0), 1).store[..48]),
            "05010100010401000040000000d1010000070200004e020000eb000000f9020000300000002d00000015020000080200");
    }
}
