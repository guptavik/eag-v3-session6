// Mock data anchored to "now" so the demo always has fresh meetings.
// Times are computed at module load; reload the popup to refresh.

const NOW = new Date();

function inHours(h) {
  const d = new Date(NOW);
  d.setHours(d.getHours() + h, 0, 0, 0);
  return d.toISOString();
}

function addMinutes(iso, m) {
  const d = new Date(iso);
  d.setMinutes(d.getMinutes() + m);
  return d.toISOString();
}

const MOCK_MEETINGS = [
  {
    id: "mtg_001",
    title: "Product Demo with Acme Corp",
    startTime: inHours(2),
    endTime: addMinutes(inHours(2), 60),
    attendees: ["john.doe@acme.com", "jane.smith@acme.com"],
    location: "Zoom",
    description: "Discuss Q1 roadmap and Enterprise pricing tier."
  },
  {
    id: "mtg_002",
    title: "1:1 with Sarah",
    startTime: inHours(5),
    endTime: addMinutes(inHours(5), 30),
    attendees: ["sarah.lee@ourcompany.com"],
    location: "Office - Room 4B",
    description: "Weekly sync."
  },
  {
    id: "mtg_003",
    title: "Vendor Review - Globex Logistics",
    startTime: inHours(24),
    endTime: addMinutes(inHours(24), 45),
    attendees: ["mike.chen@globex.io", "procurement@ourcompany.com"],
    location: "Google Meet",
    description: "Review SLA performance and renewal terms for Globex contract."
  },
  {
    id: "mtg_004",
    title: "Engineering Planning",
    startTime: inHours(28),
    endTime: addMinutes(inHours(28), 90),
    attendees: ["eng-leads@ourcompany.com"],
    location: "Zoom",
    description: "Q2 roadmap planning."
  },
  {
    id: "mtg_005",
    title: "Intro Call - Initech",
    startTime: inHours(48),
    endTime: addMinutes(inHours(48), 30),
    attendees: ["bill.lumbergh@initech.com"],
    location: "Zoom",
    description: "Initial discovery call about workflow automation needs."
  },
  {
    id: "mtg_006",
    title: "Board Update Prep",
    startTime: inHours(72),
    endTime: addMinutes(inHours(72), 60),
    attendees: ["ceo@ourcompany.com", "cfo@ourcompany.com"],
    location: "Office - Boardroom",
    description: "Prep for Friday board meeting."
  }
];

const MOCK_EMAILS = [
  {
    id: "email_001",
    subject: "Re: Product Demo Preparation",
    from: "john.doe@acme.com",
    to: "you@ourcompany.com",
    date: new Date(NOW.getTime() - 5 * 86400000).toISOString().slice(0, 10),
    snippet: "Looking forward to the demo. Our team is most interested in the pricing tiers and SSO support. Jane will join from product side.",
    keywords: ["acme", "demo", "pricing", "sso"]
  },
  {
    id: "email_002",
    subject: "Acme Corp - Pricing Questions",
    from: "jane.smith@acme.com",
    to: "you@ourcompany.com",
    date: new Date(NOW.getTime() - 3 * 86400000).toISOString().slice(0, 10),
    snippet: "Can you walk us through the Enterprise tier? We have 500 seats and need SAML.",
    keywords: ["acme", "pricing", "enterprise", "saml"]
  },
  {
    id: "email_003",
    subject: "Globex SLA - Q4 numbers",
    from: "mike.chen@globex.io",
    to: "procurement@ourcompany.com",
    date: new Date(NOW.getTime() - 7 * 86400000).toISOString().slice(0, 10),
    snippet: "Attaching the Q4 SLA report. We hit 99.7% uptime, slightly below the 99.9% target. Happy to discuss.",
    keywords: ["globex", "sla", "uptime", "vendor"]
  },
  {
    id: "email_004",
    subject: "Initech intro - context",
    from: "referral@partner.com",
    to: "you@ourcompany.com",
    date: new Date(NOW.getTime() - 2 * 86400000).toISOString().slice(0, 10),
    snippet: "Connecting you with Bill at Initech. They are evaluating workflow automation tools and have a $200k budget.",
    keywords: ["initech", "workflow", "automation", "intro"]
  },
  {
    id: "email_005",
    subject: "Board deck draft",
    from: "cfo@ourcompany.com",
    to: "you@ourcompany.com",
    date: new Date(NOW.getTime() - 1 * 86400000).toISOString().slice(0, 10),
    snippet: "Draft of the board deck attached. Please review the revenue slides before our prep call.",
    keywords: ["board", "deck", "revenue"]
  }
];

const MOCK_PEOPLE = {
  "john.doe@acme.com": {
    name: "John Doe",
    currentRole: "VP of Engineering",
    company: "Acme Corp",
    background: "10 years at Acme, previously Senior SWE at Google Cloud. Leads infra and platform teams.",
    linkedInUrl: "https://linkedin.com/in/johndoe-acme"
  },
  "jane.smith@acme.com": {
    name: "Jane Smith",
    currentRole: "Director of Product",
    company: "Acme Corp",
    background: "Joined Acme 3 years ago from Stripe. Owns billing and growth product surface.",
    linkedInUrl: "https://linkedin.com/in/janesmith-acme"
  },
  "mike.chen@globex.io": {
    name: "Mike Chen",
    currentRole: "Account Executive",
    company: "Globex Logistics",
    background: "Long-time AE at Globex, manages enterprise renewals.",
    linkedInUrl: "https://linkedin.com/in/mikechen-globex"
  },
  "bill.lumbergh@initech.com": {
    name: "Bill Lumbergh",
    currentRole: "Director of Operations",
    company: "Initech",
    background: "20+ years at Initech. Owns internal tooling and process automation initiatives.",
    linkedInUrl: "https://linkedin.com/in/blumbergh"
  },
  "sarah.lee@ourcompany.com": {
    name: "Sarah Lee",
    currentRole: "Engineering Manager",
    company: "OurCompany",
    background: "Internal — your direct report. Manages the platform team.",
    linkedInUrl: null
  }
};

const MOCK_COMPANIES = {
  "acme corp": {
    title: "Acme Corp - Company Profile",
    snippet: "B2B SaaS company, ~500 employees. Recently raised $50M Series C led by Sequoia. Known for workflow automation and a strong enterprise customer base.",
    url: "https://acme.com/about",
    linkedInCompanyUrl: "https://linkedin.com/company/acme-corp",
    recentNews: [
      "Acme raises $50M Series C (TechCrunch, last month)",
      "Acme launches SSO/SAML for Enterprise tier (Acme blog, 2 weeks ago)"
    ]
  },
  "globex logistics": {
    title: "Globex Logistics - Company Profile",
    snippet: "Mid-market logistics SaaS, ~200 employees. Focus on shipment tracking and SLA reporting. Q3 had reliability issues per industry reports.",
    url: "https://globex.io/about",
    linkedInCompanyUrl: "https://linkedin.com/company/globex-logistics",
    recentNews: [
      "Globex announces new datacenter in Frankfurt (Globex blog, last quarter)"
    ]
  },
  "initech": {
    title: "Initech - Company Profile",
    snippet: "Mid-size enterprise (~1,200 employees) in financial services. Heavy on legacy mainframe systems, currently modernizing internal workflows.",
    url: "https://initech.com/about",
    linkedInCompanyUrl: "https://linkedin.com/company/initech",
    recentNews: [
      "Initech announces $5M digital transformation initiative (PR Newswire, 6 weeks ago)"
    ]
  }
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = { MOCK_MEETINGS, MOCK_EMAILS, MOCK_PEOPLE, MOCK_COMPANIES };
}
