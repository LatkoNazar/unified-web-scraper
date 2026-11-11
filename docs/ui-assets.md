# Графіка та брендинг інтерфейсу

Інтерфейс використовує редакційну стилістику власного `brand-website`:
теплий папір `#eeeee5`, чорний `#10110e`, оливковий `#59664b`, помаранчевий
`#c85223`, тонкі лінії та читабельний текст даних.
Дизайн-референси користувача задають напрям, а не копію готового макета.

| Раніше | Зміна | Мета |
| --- | --- | --- |
| Однакова вага заголовків, форми й параметрів | Один основний запит і кнопка; технічні параметри в додаткових налаштуваннях | Зрозумілий перший крок |
| Типові округлі панелі та сині акценти | Папір, чорна сітка, локальна типографіка, стримані кольори | Цілісний власний стиль |
| Великий декоративний вступ і повторювані пояснення | Компактний запит без hero й слоганів | Більше місця датасету |
| Ціна дрібним текстом у статистиці | Сума й зупинка у верхній закріпленій панелі | Контроль витрат під час збору |
| Змішані дії результатів | Окремі перемикачі вигляду, фільтри й колонки | Простіше порівнювати записи |

## Згенерований asset

- Файл: `src/unified_web_parser/interfaces/static/assets/field-scan.png`.
- Призначення: збережена графіка попереднього оформлення; поточний компактний UI її не завантажує. Це не фото товару й не дані.
- Метод: вбудований `image_gen`, генерація нового зображення.
- Формат: PNG, 1536 × 1024; оригінал залишений без змін.
- Копія генерації: `C:/Users/Nazar Latko/.codex/generated_images/01a16343-299a-75f1-92ec-f71c99afe0b4/exec-2f6f04a7-030a-49b9-a64b-4d7c7727e063.png`.

Фінальний prompt:

```text
Use case: stylized-concept. Asset type: original decorative editorial artwork for a working web data discovery application, not a UI mockup. Create a premium analog technical illustration of scattered information resolving into an ordered field: one sculptural folded ribbon of many paper-thin layers, a loose toroidal / topographic form with intricate black halftone stippling and precise parallel contour lines. Small fragments of thin rectangular grid sheets at one side become orderly flowing layers. Inspired by Swiss technical editorial printing and monochrome point-cloud scientific illustrations, with subtle print texture, restrained and architecturally elegant. Warm ivory paper background #eeeee5, near-black #10110e linework, a single small burnt orange #c85223 sphere as a visual focal point and very subtle muted olive #59664b detail. A clean isolated composition, wide 3:2 landscape, generous breathing room, object mostly centered and fully visible; no edge clipping. Very sharp detailed fine dots and thin lines, light airy negative space. This will sit beside large live HTML typography in the search application's introduction. No letters, numbers, words, labels, logos, watermark, interface frames, dashboards, fake controls, product photos, people, or gradients. Do not reproduce an existing reference composition; make original artwork.
```

## Шрифти

Шрифти скопійовані з `brand-website/public/assets/fonts/`; запитів до зовнішніх
font CDN немає. Ліцензії SIL Open Font License збережені поряд із файлами:

- `assets/fonts/inter-variable.woff2` — основний текст, поля й таблиці.
- `assets/fonts/roboto-condensed-variable.woff2` — заголовки.
- `assets/fonts/Inter-OFL.txt` і `Roboto-Condensed-OFL.txt` — ліцензії.

Повні шляхи — відносно `src/unified_web_parser/interfaces/static/`.
Іконки, текст, кнопки й таблиці залишаються HTML/CSS; у зображення не вбудовано
інтерактивні елементи або інтерфейсний текст.
