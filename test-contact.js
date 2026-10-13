const API_URL = "http://127.0.0.1:5000/contact";
const SITE_ORIGIN = "https://kaarisilta.oivallus.fi";

const formData = {
  siteId: "kaarisilta",
  name: "Testikäyttäjä",
  email: "test@example.com",
  message: "Tämä on yhteydenottolomakkeen testiviesti.",
};

async function sendTestContact() {
  const response = await fetch(API_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: SITE_ORIGIN,
    },
    body: JSON.stringify(formData),
  });

  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error(`Palvelin palautti virheellisen vastauksen (${response.status})`);
  }

  if (!response.ok) {
    throw new Error(
      `Lähetys epäonnistui (${response.status}): ${result.error ?? "Tuntematon virhe"}`,
    );
  }

  console.log("Testiviesti lähetettiin onnistuneesti:", result);
}

sendTestContact().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
