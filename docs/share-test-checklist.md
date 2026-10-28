# Share-menu test: "Share to BedahReview" on a real phone

Goal: find out, on real phones with the real Shopee and Tokopedia apps, whether the Share button can send review text into BedahReview. Today the app says "not tested yet" and tells people to use Paste. Your results decide what the app should say.

Allow about 30 minutes. You need: the live Cloud Run link (it must be https), the access code, an Android phone with Chrome, an iPhone with Safari, and the Shopee and Tokopedia apps on both. Write what happened, even when it is boring or "nothing happened". "It does not work" is a useful result.

Do not screenshot buyer names. Crop them out or skip the screenshot.

## Android (Chrome)

| # | Do this | Look for | Result |
|---|---|---|---|
| A1 | In Chrome open the live link and sign in. | The page loads and looks right at phone width. | |
| A2 | Menu (three dots) > **Install app**. If you only see "Add to home screen", note that, and choose it. | Is "Install app" offered? Does an icon appear on the home screen? (Share only works for an installed app, not a bookmark.) | |
| A3 | Open BedahReview from the icon once, then close it. | It opens full screen, without the Chrome address bar. | |
| A4 | In **Shopee**, open a product's reviews. Press and hold a review's text. | Does a selection appear? Is there a **Share** option, or only Copy? | |
| A5 | Tap Share. Scroll the share sheet, and tap "More" if there is one. | Is **BedahReview** in the list? Which row or screen did you find it on? | |
| A6 | Tap BedahReview. | Does the app open? Is the review text in the **Tempel** box? Does the green note "Teks dari menu Bagikan sudah masuk" show? | |
| A7 | Look at the text that arrived. | Is it the whole review? Anything extra added by Shopee (a link, "Check out this review")? Anything missing? | |
| A8 | Tap **Bedah ulasan**. | Do you get results? | |
| A9 | Repeat A4 to A8 in **Tokopedia**. | Same questions. | |
| A10 | In either app use the **product** Share button (the link, not a review). Pick BedahReview. | Expected: a note "Yang dikirim hanya tautan, bukan teks ulasan". Is that what you see? | |
| A11 | Can you select **several** reviews at once and share them together? | How many reviews can one Share carry: one, a page, none? | |
| A12 | Fallback: copy a review, open BedahReview, paste into Tempel. | Works? | |

## iPhone (Safari)

Expected, from what we know: iPhone does not let a website be a Share target, so BedahReview will not appear in the share sheet. This test confirms it on your phone.

| # | Do this | Look for | Result |
|---|---|---|---|
| I1 | In Safari open the live link and sign in. | The page loads and looks right. | |
| I2 | Share button > **Add to Home Screen**. | The icon appears. It opens full screen. | |
| I3 | In **Shopee**, select a review's text and open Share. | Is BedahReview listed (even under "More" or "Edit actions")? | |
| I4 | Repeat in **Tokopedia**. | Same. | |
| I5 | Fallback: copy a review, open the home screen icon, paste into Tempel, tap Bedah ulasan. | Works? Does the keyboard cover the Bedah ulasan button? | |

## Also worth a glance on both phones

- Indonesian and English both switch and look right. Dark mode looks right.
- The bottom bar (Bedah ulasan, Pesaing) does not cover content.
- "Keluar" (sign out) at the bottom of the page works and the sign-in page returns.

## What to send me

Paste the Result column for each row, plus: phone model, Android or iOS version, Chrome or Safari version, and the Shopee and Tokopedia app versions. Add anything surprising.

## What I will change in the app from your answers

- If sharing a review works on Android in one or both apps: the note "Belum diuji dengan Shopee atau Tokopedia di HP asli" is replaced by what you saw, per app, and the three steps are corrected to match the real taps.
- If the Share option is missing in the apps, or BedahReview is not listed: the Share tab becomes a short "this does not work with Shopee yet, use Paste" message with the paste steps first.
- If text arrives with extra words (links, promo lines): the app can trim them, and I will need one example of the exact text.
- If the iPhone shows BedahReview: I will remove the iPhone warning. If it does not, the warning stays, with the home screen install steps.
- If sharing only carries one review at a time: the page will say so, so sellers are not surprised.
