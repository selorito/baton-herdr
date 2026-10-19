# baton: kurulum ve kullanım rehberi

Bu rehber baton'u kendi bilgisayarına kurup kullanacak herkes içindir. Kodlama bilgisi
gerektiren tek yer, ajanlara verdiğin görevlerin kendisi.

## baton ne yapar?

baton, kodlama ajanlarını (Claude Code, Codex CLI, OpenCode) senin yerine çalıştırır ve
izler:

- Görevleri ajanlara paralel dağıtır: varsayılan olarak her ajandan iki oturum, en fazla
  dört görev aynı anda çalışır (aynı klasörde ise yalnızca biri).
- Bir ajanın kullanım limiti dolunca görevi diğer ajana devreder. Limit açılınca aynı
  oturumu kaldığı yerden sürdürür.
- Ajan takılırsa ya da çökerse oturumunu yeniden açar.
- Ajan bir şey sorarsa ya da izin isterse Telegram'dan sana sorar. Okuma komutları gibi
  zararsız izinleri kendisi verir.
- Telegram'dan "bana şunu yap" dediğinde işi planlatır, sen onaylayınca adım adım
  yaptırır ve sonucu ekran görüntüsüyle geri gönderir.

## Gerekenler

- Linux. Ubuntu 22.04 ve sonrası denendi.
- [uv](https://docs.astral.sh/uv/getting-started/installation/):
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [herdr](https://github.com/herdrdev/herdr) 0.9.1 veya sonrası.
- Kullanacağın ajanlar. Her biri **bir kez senin tarafından giriş yapılmış** olmalı:
  - Claude Code: `claude` yazıp giriş yap.
  - Codex CLI: `codex` yazıp giriş yap.
  - OpenCode: `opencode` (isteğe bağlı).
- Telegram hesabı. İsteğe bağlı, ama gözetimsiz kullanım için şart.

## Kurulum

```bash
uv tool install git+https://github.com/selorito/baton-herdr
baton setup
```

`baton setup` sana adım adım soru sorar:

1. **baton-detect.** Yoksa indirmeyi önerir. Bu, baton'un ajan ekranlarını okuyan
   parçası.
2. **herdr entegrasyonları.** Bulduğu her ajan için kurar.
3. **Ayar dosyası.** Bulduğu ajanlar ve saat dilimin yazılır:
   `~/.config/baton/baton.toml`.
4. **Telegram:**
   - Telegram'da **@BotFather**'a yaz, `/newbot` ile bir bot oluştur, verdiği token'ı
     yapıştır. Token ekranda görünmez; yalnızca senin okuyabildiğin bir dosyaya kaydedilir.
   - Sonra telefonundan **kendi botuna** herhangi bir mesaj at. baton seni bu mesajdan
     tanır. Bundan sonra botu yalnızca sen kullanabilirsin.
5. **İzin politikası.** Ajanların hangi klasörlerde test ve derleme komutlarını sormadan
   çalıştırabileceği. Yalnızca **kendi** depolarını yaz.
6. **Servis.** baton arka planda çalışır, bilgisayar açılınca kendiliğinden başlar.

Sonunda `baton doctor` her şeyi kontrol eder. `fail` ya da `warn` satırlarının altında ne
yapman gerektiği yazar. `baton setup`'ı istediğin kadar tekrar çalıştırabilirsin; var olan
ayarları bozmaz.

## Kullanım

### Bilgisayardan görev vermek

```bash
baton task add "kısa başlık" -C ~/projelerim/depo -i "Ajana ne yapmasını istediğin, açıkça."
baton status                  # görevler ve durumları
herdr --session baton         # ajanları çalışırken izle (çıkmak: herdr'ın kısayolu)
baton task cancel t-1a2b3c4d  # bir görevi bırak
```

### Telegram'dan proje başlatmak

```
/new kafem için tek sayfalık bir web sitesi, menü ve çalışma saatleriyle
```

1. **📋 Plan ready** gelir: bir ajan isteği en fazla 8 adıma böler. **Start**'a basmadan
   hiçbir şey çalışmaz. Beğenmezsen **Cancel**.
2. Birbirine bağlı olmayan adımlar aynı anda, farklı ajanlarda ve her biri kendi git
   worktree'sinde yapılır; biten adım proje klasörüne birleştirilir (planda "(at once)" ve
   "(after 1, 2)" yazar). Her adım bitince sessiz bir bildirim gelir. Birleştirme çakışırsa
   bir ajan çakışmayı çözer; çözemezse proje durur ve sana haber verilir.
3. **✅ Project done** gelir: değişen dosyalar, önizleme linki ve sayfanın ekran görüntüsü.

Projeler `~/dev/baton-projects/` altında açılır.

Adımların daha hızlı bitmesi ve limitin daha yavaş dolması için adımları daha hızlı bir
modelle çalıştırabilirsin; planı yine varsayılan model yazar. `baton.toml` içine:

```toml
[scheduler.models]
claude = { step = "sonnet", merge = "sonnet" }
```

### Telegram komutları

| Komut | Ne yapar |
|---|---|
| `/new <istek>` | Yeni proje başlatır |
| `/status` | Açık görevler ve ajanların durumu |
| `/budget` | Her ajanın kalan kullanım hakkı |
| `/cancel t-…` | Bir görevi bırakır, ajanını kapatır |
| `/cancel p-…` | Bir projenin tamamını bırakır |
| `/help` | Bu liste |

### Gelen mesajlar ve ne yapacağın

Yalnızca senden bir şey bekleyen mesajlar **sesli** gelir; diğerleri sessizdir.

| Başlık | Anlamı | Ne yaparsın |
|---|---|---|
| 🔐 Needs approval | Ajan riskli bir komut için izin istiyor | **Approve** ya da **Deny** |
| ❓ Needs answer | Ajan bir şey soruyor | Mesajı **uzun bas → Yanıtla**, cevabını yaz |
| ❗ Needs you | Telefondan cevaplanamayan bir ekran | Bilgisayardan bak (`herdr --session baton`) ya da **Cancel task** |
| 📋 Plan ready | Proje planı hazır | **Start** ya da **Cancel** |
| ⏸ Stalled | Ajan 15 dakikadır kımıldamıyor | Bir şey yapman gerekmez; baton oturumu yeniden açar |
| ⛔ All agents limited | Bütün ajanların limiti dolu | Bir şey yapman gerekmez; limit açılınca devam eder |
| ⚠ Project stopped | Bir proje adımı tamamlanamadı | Klasöre bak; gerekirse isteği yeniden gönder |

Yanıtla demeden düz mesaj yazarsan mesaj hiçbir ajana gitmez; bot bunu hatırlatır.

## Gözetimsiz çalıştırma için öneriler

- **Güvendiğin depoları policy'ye yaz.** `~/.config/baton/policy.yaml` dosyasındaki
  `trusted_dirs` listesinde olmayan bir klasörde ajan her test komutu için sana sorar.
  Bu klasörlerde ajanların "bu klasöre güveniyor musun?" sorusunu da baton cevaplar.
- **Telefonundan önizleme linklerini açmak istersen** `baton.toml`'a şunu ekle:

  ```toml
  [projects]
  preview_host = "0.0.0.0"
  ```

  Aynı ağdaki herkes projelerin sayfalarını görebilir; okul ya da ortak ağda dikkatli ol.
- **Ajanların güncelleme ekranları.** Codex açılışta güncelleme sorarsa baton bunu
  "Skip" ile geçer. Ajanları kendin, ara sıra güncelle:
  `npm install -g @openai/codex`, `claude update`.
- **Bitmiş görevlerin ajanları kapatılır** (her biri birkaç yüz MB bellek tutar).
  Konuşma ajanın kendi kayıtlarında kalır. Açık kalsın istersen `baton.toml`'da
  `[scheduler] close_finished_panes = false` yaz.

## Sorun giderme

| Durum | Ne yap |
|---|---|
| Herhangi bir sorun | `baton doctor`: neyin eksik olduğunu ve nasıl düzeltileceğini söyler |
| baton ne yapıyor? | `journalctl --user -u baton -f` |
| Ajanı görmek istiyorum | `herdr --session baton` |
| Telegram'dan mesaj gelmiyor | `baton doctor`'da `telegram` satırı. Bilgisayar internete bağlı mı? |
| Bot butonlara tepki vermiyor | Bilgisayar yeni açıldıysa ağ gelince bot kendiliğinden bağlanır; bir dakika bekle |
| Önizleme linki açılmıyor | Telefon aynı ağda mı, `preview_host = "0.0.0.0"` mı? `baton doctor` → `project preview` |
| Görev takıldı, ne olduğu belli değil | `herdr --session baton` ile bak; gerekirse `/cancel t-…` |
| baton'u durdur / başlat | `systemctl --user stop baton` / `systemctl --user start baton` |

Bir hata bulursan `baton doctor` çıktısını ve `journalctl --user -u baton --since "1 hour
ago"` çıktısının ilgili kısmını paylaş. Bu çıktılarda token görünmez.

## Güncelleme ve kaldırma

```bash
uv tool upgrade baton-herdr && baton setup            # güncelle, ayarları gözden geçir
systemctl --user disable --now baton baton-herdr      # kaldırmadan önce servisi durdur
uv tool uninstall baton-herdr
```

Ayarların `~/.config/baton/`, görev geçmişin `~/.local/share/baton/` altında kalır; istersen
elle silebilirsin.

### Codex "oturum kapalı" derse

Telegram'da "codex is signed out" bildirimi gelirse Codex'in girişi düşmüştür; işler o
sırada diğer ajana geçer. Terminalde:

```bash
codex logout && codex login
```
